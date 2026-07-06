"""Defined-risk 0DTE CREDIT SPREAD backtester + walk-forward (premium SELLING).

Rationale: buying 0DTE premium loses to theta+spread (proven — see walkforward.py). Selling a
defined-risk vertical flips the sign of theta: you collect the decay. Bullish signal -> sell a
bull PUT spread (below price); bearish -> sell a bear CALL spread (above price). Max loss is
capped at (width - credit), so position sizing is by that max loss.

Everything uses REAL Polygon option bars for BOTH legs. Fills cross the spread (slippage) and
pay commission on all four legs (open + close, two legs each).

    python -m src.research.spreads --days 90 --train 20 --test 5 --quantile 0.15
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd

from ..backtest import ET
from ..common import MarketSnapshot, Signal
from ..data.data_pipeline import has_vix, load_bars
from ..data.polygon_options import PolygonOptions
from ..learning.evaluator import summarize
from ..signals.feature_engineering import build_features
from ..signals.labeling import make_breach_labels, make_labels, make_range_labels
from ..signals.lightgbm_model import DirectionalClassifier
from ..signals.range_model import RangeForecaster, dynamic_short_otm
from ..signals.regime_classifier import classify_regime
from ..signals.signal_generator import SignalGenerator
from ..execution.position_manager import PositionManager
from ..execution.risk import defense_triggered, spread_ev, stop_cost
from ..utils.config import load_config
from ..utils.logger import get_logger

log = get_logger("spreads")


@dataclass
class SpreadTrade:
    open_time: datetime
    close_time: datetime
    kind: str            # bull_put | bear_call
    credit: float
    exit_cost: float
    quantity: int
    exit_reason: str
    commission: float

    @property
    def pnl(self) -> float:
        return (self.credit - self.exit_cost) * 100 * self.quantity - self.commission

    @property
    def hold_minutes(self) -> float:
        return (self.close_time - self.open_time).total_seconds() / 60.0

    @property
    def won(self) -> bool:
        return self.pnl > 0


def _parse(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def build_spread(poly: PolygonOptions, expiry, kind: str, spot: float, cfg,
                 otm: float | None = None) -> dict | None:
    """Resolve real short/long legs from the real listed chain. None if strikes unavailable.
    `otm` overrides the static short-strike distance (used by the smart/range mode)."""
    width = cfg.spread.width
    otm = cfg.spread.short_otm_pct if otm is None else otm
    chain = poly.list_contracts(expiry)
    if kind == "bull_put":
        side = chain[chain["type"] == "P"].sort_values("strike")
        target = spot * (1 - otm)
        below = side[side["strike"] <= target]
        if below.empty:
            return None
        short = below.iloc[-1]
        long_target = short["strike"] - width
        longs = side[side["strike"] <= long_target]
        if longs.empty:
            return None
        long = longs.iloc[-1]
    else:  # bear_call
        side = chain[chain["type"] == "C"].sort_values("strike")
        target = spot * (1 + otm)
        above = side[side["strike"] >= target]
        if above.empty:
            return None
        short = above.iloc[0]
        long_target = short["strike"] + width
        longs = side[side["strike"] >= long_target]
        if longs.empty:
            return None
        long = longs.iloc[0]
    if short["ticker"] == long["ticker"]:
        return None
    return {"kind": kind, "short_ticker": short["ticker"], "long_ticker": long["ticker"],
            "short_strike": float(short["strike"]), "long_strike": float(long["strike"]),
            "width": abs(float(short["strike"]) - float(long["strike"]))}


def _aligned_legs(poly, spread, expiry, max_staleness="5min"):
    """Align both legs' REAL bars onto the short leg's timestamps.

    Audit M1: unlimited forward-fill let a long leg that hadn't traded for an hour price
    the spread with a stale print. merge_asof with a hard staleness tolerance drops any
    row where the long leg has no trade within `max_staleness` — those moments are
    unpriceable, not 'free to assume unchanged'. Carries high/low for both legs so stops
    can be checked pessimistically (audit M2)."""
    sb = poly.option_bars(spread["short_ticker"], expiry)
    lb = poly.option_bars(spread["long_ticker"], expiry)
    if sb.empty or lb.empty:
        return None
    s = sb[["close", "high", "low"]].rename(
        columns={"close": "s_close", "high": "s_high", "low": "s_low"})
    l = lb[["close", "high", "low"]].rename(
        columns={"close": "l_close", "high": "l_high", "low": "l_low"})
    df = pd.merge_asof(s.sort_index(), l.sort_index(), left_index=True, right_index=True,
                       direction="backward", tolerance=pd.Timedelta(max_staleness))
    df = df.dropna()
    return df if not df.empty else None


def simulate(cfg, bars, features, probs, poly, include_vix, allow_dates=None,
             range_preds=None, breach_preds=None):
    """`range_preds`: optional per-bar expected-range forecasts (fraction of spot). When
    given, short strikes are placed beyond range x safety (skip when > cap) and a
    strike-defense exit closes the spread if SPY reaches the short strike — mirroring the
    live loop's intelligence layer.
    `breach_preds`: optional (p_dn, p_up) per-bar arrays for the EV gate: skip entries
    where credit - width x P(breach) < intelligence.min_ev."""
    siggen = SignalGenerator(cfg)
    pm = PositionManager(cfg)
    equity = float(cfg.risk["account"]["starting_equity"])
    risk_pct = cfg.risk["per_trade"]["risk_pct"]
    max_ct, min_ct = cfg.risk["per_trade"]["max_contracts"], cfg.risk["per_trade"]["min_contracts"]
    slip = cfg.risk["commissions"]["slippage_frac"]
    comm = cfg.risk["commissions"]["per_contract"]
    open_t, no_new_t = _parse(cfg.session.market_open), _parse(cfg.session.no_new_trades_after)
    flatten_t = _parse(cfg.session.flatten_time)
    # Spreads hold for theta — use the spread-specific max hold, NOT the scalper's 10-min stop.
    time_stop = timedelta(minutes=cfg.spread.get("max_hold_minutes", 240))
    pt, stop_mult = cfg.spread.profit_target_frac, cfg.spread.stop_mult
    min_credit = cfg.spread.min_credit

    idx_utc = bars.index
    local = idx_utc.tz_convert(ET)
    highs, lows, closes, vols = (bars["high"].to_numpy(), bars["low"].to_numpy(),
                                 bars["close"].to_numpy(), bars["volume"].to_numpy())
    trades: list[SpreadTrade] = []
    i, n = 0, len(bars)
    while i < n:
        now = local[i].to_pydatetime()
        t = now.time()
        if not (open_t <= t < no_new_t) or (allow_dates is not None and now.date() not in allow_dates):
            i += 1
            continue
        frow = features.iloc[i]
        price = float(closes[i])
        snap = MarketSnapshot(
            timestamp=idx_utc[i].to_pydatetime(), spy_price=price, spy_volume=float(vols[i]),
            vwap=price * (1 + float(frow["vwap_dev"])), atr_5min=float(frow["atr_5"]),
            rvol=float(frow["rvol"]),
            high_5min=float(highs[max(i - 5, 0):i].max()) if i else price,
            low_5min=float(lows[max(i - 5, 0):i].min()) if i else price,
            vix=float(frow.get("vix", 0.0)) if include_vix else float("nan"),
            regime=classify_regime(frow), ml_prob_up=float(probs[i]), features=frow.to_dict())

        decision = siggen.generate(snap)
        if decision.signal == Signal.NO_TRADE:
            i += 1
            continue
        ok, _ = pm.can_open(now, equity, 0)
        if not ok:
            i += 1
            continue

        kind = "bull_put" if decision.signal == Signal.BUY_CALL else "bear_call"
        expiry = now.date()
        otm = None
        if range_preds is not None and cfg.intelligence.get("use_range_strikes", True):
            otm = dynamic_short_otm(cfg.spread.short_otm_pct, float(range_preds[i]),
                                    cfg.intelligence.get("range_safety_mult", 1.25),
                                    cfg.intelligence.get("max_short_otm_pct", 0.01))
            if otm is None:   # forecast says no safe strike close enough — skip
                i += 1
                continue
        spread = build_spread(poly, expiry, kind, price, cfg, otm=otm)
        if spread is None:
            i += 1
            continue
        legs = _aligned_legs(poly, spread, expiry)
        if legs is None:
            i += 1
            continue
        entry_ts = idx_utc[i]
        fwd = legs.loc[legs.index >= entry_ts]
        if fwd.empty:
            i += 1
            continue
        e = fwd.iloc[0]
        credit = e["s_close"] * (1 - slip) - e["l_close"] * (1 + slip)  # net premium received
        if credit < min_credit:
            i += 1
            continue
        if breach_preds is not None:
            p_b = float(breach_preds[0][i] if kind == "bull_put" else breach_preds[1][i])
            if spread_ev(credit, p_b, pt, stop_mult) < cfg.intelligence.get("min_ev", 0.0):
                i += 1
                continue
        max_loss_per = (spread["width"] - credit) * 100
        if max_loss_per <= 0:
            i += 1
            continue
        qty = int((risk_pct * equity) // max_loss_per)
        qty = max(min_ct, min(max_ct, qty))
        pm.on_open()

        entry_bar_ts = fwd.index[0]
        day_flat = pd.Timestamp(datetime.combine(expiry, flatten_t), tz=ET).tz_convert("UTC")
        deadline = min(entry_bar_ts + time_stop, day_flat)
        walk = legs.loc[(legs.index > entry_bar_ts) & (legs.index <= deadline)]

        exit_cost, reason, exit_ts = None, None, entry_bar_ts
        tp_cost = credit * (1 - pt)
        sl_cost = stop_cost(credit, spread["width"], stop_mult,
                            cfg.spread.get("stop_width_frac"))
        spy_close = bars["close"]
        buffer_pct = cfg.intelligence.get("defense_buffer_pct", 0.001)
        for ots, row in walk.iterrows():
            # Audit M2: stops trigger on the PESSIMISTIC intrabar cost (short at its high,
            # long at its low) — the worst price the bar could have handed us. Profit
            # targets stay on closes (never award an optimistic intrabar touch).
            cost = row["s_close"] * (1 + slip) - row["l_close"] * (1 - slip)
            cost_worst = row["s_high"] * (1 + slip) - row["l_low"] * (1 - slip)
            if cost <= tp_cost:
                exit_cost, reason, exit_ts = tp_cost, "take_profit", ots
                break
            if range_preds is not None and cfg.intelligence.get("defense_enabled", True):
                spot = float(spy_close.asof(ots))
                if spot == spot and defense_triggered(kind, spot, spread["short_strike"],
                                                      buffer_pct):
                    exit_cost, reason, exit_ts = cost, "strike_defense", ots
                    break
            if cost_worst >= sl_cost:
                # Filled at the stop level or the bar's worst, whichever is uglier — a
                # stop in a gapping bar does not fill politely at the stop price.
                exit_cost = min(max(sl_cost, cost), spread["width"])
                reason, exit_ts = "stop_loss", ots
                break
        if exit_cost is None:
            last = legs.loc[legs.index <= deadline].iloc[-1]
            exit_cost = max(last["s_close"] * (1 + slip) - last["l_close"] * (1 - slip), 0.0)
            reason = "flatten" if deadline == day_flat else "time_stop"
            exit_ts = legs.loc[legs.index <= deadline].index[-1]

        tr = SpreadTrade(entry_bar_ts.to_pydatetime(), exit_ts.to_pydatetime(), kind,
                         round(credit, 2), round(exit_cost, 2), qty, reason,
                         commission=comm * qty * 4)
        trades.append(tr)
        equity += tr.pnl
        pm.record_result(tr.pnl)
        j = int(idx_utc.searchsorted(exit_ts, side="right"))
        i = max(j, i + 1)
    return trades, equity


def run(cfg, days=90, train_win=20, test_win=5, quantile=None, verbose=True, smart=False,
        ev_gate=False, shuffle_probs=False):
    """`smart=True` mirrors the live intelligence layer: per-fold range forecaster trained
    on the fold's train window drives dynamic strike placement + strike-defense exits.
    `ev_gate=True` trains per-fold breach classifiers and skips entries whose credit does
    not cover the modeled breach risk (the premium-richness gate)."""
    poly = PolygonOptions.from_config(cfg)
    bars = load_bars(cfg, days, download=False, poly=poly)
    include_vix = has_vix(bars)
    features = build_features(bars, include_vix=include_vix)
    mp = cfg.model_params
    labels, valid = make_labels(bars["close"], horizon_bars=mp["label"]["horizon_bars"],
                                threshold_pct=mp["label"]["threshold_pct"])
    range_y, range_valid = make_range_labels(
        bars["high"], bars["low"], bars["close"],
        horizon_bars=cfg.intelligence.get("range_horizon_bars", 60)) if smart else (None, None)
    if ev_gate:
        b_dn, b_up, b_valid = make_breach_labels(
            bars["high"], bars["low"], bars["close"],
            horizon_bars=cfg.intelligence.get("breach_horizon_bars", 120),
            threshold_pct=cfg.spread.short_otm_pct)
    et_dates = np.array(bars.index.tz_convert(ET).date)
    finite = features.notna().all(axis=1).to_numpy() & np.isfinite(features.to_numpy()).all(axis=1)
    base_mask = valid.to_numpy() & finite
    dates = sorted(set(bars.index.tz_convert(ET).date))

    all_oos, fold_rows = [], []
    start = train_win
    while start < len(dates):
        train_dates = set(dates[start - train_win:start])
        test_dates = set(dates[start:start + test_win])
        if not test_dates:
            break
        m = base_mask & np.isin(et_dates, list(train_dates))
        X, y = features[m], labels[m]
        if len(X) < 100 or y.nunique() < 2:
            start += test_win
            continue
        clf = DirectionalClassifier(params=mp["lightgbm"], feature_columns=list(features.columns))
        clf.train(X, y, valid_fraction=mp["train"]["valid_fraction"],
                  num_boost_round=mp["train"]["num_boost_round"],
                  early_stopping_rounds=mp["train"]["early_stopping_rounds"])
        if quantile is not None:
            tp = clf.predict_proba(X)
            cfg.signal._data["ml_threshold_long"] = float(np.quantile(tp, 1 - quantile))
            cfg.signal.ml_threshold_long = cfg.signal._data["ml_threshold_long"]
            cfg.signal._data["ml_threshold_short"] = float(np.quantile(tp, quantile))
            cfg.signal.ml_threshold_short = cfg.signal._data["ml_threshold_short"]
        probs = clf.predict_proba(features)
        if shuffle_probs:
            # DIAGNOSTIC ONLY (random-entry benchmark): destroy any information link
            # between model and market while preserving the probability distribution,
            # threshold structure, and entry frequency. If shuffled ~= real, the entry
            # stack carries no information; if shuffled > real, entries are ANTI-timed.
            probs = np.random.default_rng(42 + start).permutation(probs)

        range_preds = None
        if smart:
            rm_mask = (range_valid.to_numpy() & finite
                       & np.isin(et_dates, list(train_dates)))
            Xr, yr = features[rm_mask], range_y[rm_mask]
            if len(Xr) >= 100:
                rf = RangeForecaster(params=mp["lightgbm"],
                                     feature_columns=list(features.columns))
                rf.train(Xr, yr, valid_fraction=mp["train"]["valid_fraction"],
                         num_boost_round=mp["train"]["num_boost_round"],
                         early_stopping_rounds=mp["train"]["early_stopping_rounds"])
                range_preds = rf.predict(features)

        breach_preds = None
        if ev_gate:
            bm_mask = (b_valid.to_numpy() & finite & np.isin(et_dates, list(train_dates)))
            Xb = features[bm_mask]
            if len(Xb) >= 100 and b_dn[bm_mask].nunique() > 1 and b_up[bm_mask].nunique() > 1:
                preds = []
                for yb in (b_dn[bm_mask], b_up[bm_mask]):
                    bc = DirectionalClassifier(params=mp["lightgbm"],
                                               feature_columns=list(features.columns))
                    bc.train(Xb, yb, valid_fraction=mp["train"]["valid_fraction"],
                             num_boost_round=mp["train"]["num_boost_round"],
                             early_stopping_rounds=mp["train"]["early_stopping_rounds"])
                    preds.append(bc.predict_proba(features))
                breach_preds = (preds[0], preds[1])

        trades, _ = simulate(cfg, bars, features, probs, poly, include_vix,
                             allow_dates=test_dates, range_preds=range_preds,
                             breach_preds=breach_preds)
        all_oos.extend(trades)
        r = summarize(trades)
        fold_rows.append({"from": min(test_dates), "to": max(test_dates),
                          "trades": r.total_trades, "win": round(r.win_rate, 2),
                          "pnl": round(r.total_pnl, 2)})
        start += test_win

    oos = summarize(all_oos)
    if verbose:
        mode = "SMART (range strikes + defense)" if smart else "BASELINE (static strikes)"
        if ev_gate:
            mode += " + EV GATE"
        print(f"\n=== CREDIT-SPREAD walk-forward OUT-OF-SAMPLE — {mode} ===")
        print(f"{len(dates)} trading days, {len(fold_rows)} folds, width=${cfg.spread.width}, "
              f"vix={'yes' if include_vix else 'no'}")
        for f in fold_rows:
            print(f"  {f['from']}..{f['to']}  trades={f['trades']:2d}  win={f['win']:.0%}  "
                  f"pnl=${f['pnl']:.2f}")
        print("-" * 60)
        print("OOS " + oos.pretty())
    return {"oos": oos.as_dict(), "folds": fold_rows}


def main():
    p = argparse.ArgumentParser(description="0DTE credit-spread walk-forward")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--train", type=int, default=20)
    p.add_argument("--test", type=int, default=5)
    p.add_argument("--quantile", type=float, default=0.15)
    p.add_argument("--no-breakout", action="store_true")
    p.add_argument("--smart", action="store_true",
                   help="use the intelligence layer: range-model strikes + defense exits")
    p.add_argument("--ev", action="store_true",
                   help="EV gate: skip entries where credit < width x P(breach) + min_ev")
    p.add_argument("--width", type=float, default=None,
                   help="override spread width in dollars (structural test, e.g. 10)")
    args = p.parse_args()
    cfg = load_config()
    if args.no_breakout:
        cfg.signal._data["require_breakout"] = False
        cfg.signal.require_breakout = False
    if args.smart:
        # --smart evaluates the full intelligence layer regardless of live defaults.
        for k in ("use_range_strikes", "defense_enabled"):
            cfg.intelligence._data[k] = True
            setattr(cfg.intelligence, k, True)
    if args.width is not None:
        cfg.spread._data["width"] = args.width
        cfg.spread.width = args.width
    run(cfg, days=args.days, train_win=args.train, test_win=args.test,
        quantile=args.quantile, smart=args.smart, ev_gate=args.ev)


if __name__ == "__main__":
    main()
