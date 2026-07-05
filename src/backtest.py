"""Event-driven backtester on REAL data.

Underlying: real SPY minute bars (Polygon). Options: real 0DTE contract minute bars (Polygon).
When a signal fires, we resolve the actual listed ATM contract for that day, enter at its real
price, and walk its real minute bars until stop-loss / take-profit / time-stop / session flatten.
There is NO Black-Scholes and NO modeled option price. Fills are simulated *against real prices*
with slippage + commission (the only unavoidable modeling in any backtest).

    python -m src.backtest --days 30      # requires POLYGON_API_KEY and cached/downloadable data
"""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd

from .common import (ExitReason, MarketSnapshot, OptionRight, Signal, TradeResult)
from .data.data_pipeline import build_training_set, has_vix, load_bars
from .data.polygon_options import PolygonOptions
from .learning.evaluator import PerformanceMonitor, summarize
from .signals.feature_engineering import build_features
from .signals.lightgbm_model import DirectionalClassifier
from .signals.regime_classifier import classify_regime
from .signals.signal_generator import SignalGenerator
from .execution.position_manager import PositionManager
from .utils.config import load_config
from .utils.logger import get_logger

log = get_logger("backtest")
ET = "America/New_York"


def _parse(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def _option_atr(obars: pd.DataFrame, upto_ts, n: int) -> float:
    """ATR of the option's own real bars up to (and including) `upto_ts`."""
    hist = obars.loc[obars.index <= upto_ts]
    if len(hist) < 2:
        return 0.0
    hist = hist.tail(n + 1)
    prev_close = hist["close"].shift(1)
    tr = pd.concat([
        hist["high"] - hist["low"],
        (hist["high"] - prev_close).abs(),
        (hist["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.iloc[1:].mean())


def run_backtest(cfg, days: int = 30, verbose: bool = True,
                 poly: PolygonOptions | None = None,
                 bars: pd.DataFrame | None = None) -> dict:
    poly = poly or PolygonOptions.from_config(cfg)
    if bars is None:
        bars = load_bars(cfg, days, download=False, poly=poly)
    if bars.empty:
        raise RuntimeError("No SPY data. Run: python -m src.data.data_pipeline --download")

    include_vix = has_vix(bars)
    features = build_features(bars, include_vix=include_vix)
    idx_utc = bars.index
    local = idx_utc.tz_convert(ET)

    if DirectionalClassifier.exists(cfg.model.path, cfg.model.meta_path):
        model = DirectionalClassifier.load(cfg.model.path, cfg.model.meta_path)
        probs = model.predict_proba(features)
        log.info("Loaded model for backtest.")
    else:
        probs = np.full(len(features), 0.5)
        log.warning("No model found; rules-only. Train first for ML signals.")

    siggen = SignalGenerator(cfg)
    pm = PositionManager(cfg)
    monitor = PerformanceMonitor()
    equity = float(cfg.risk["account"]["starting_equity"])

    open_t, no_new_t = _parse(cfg.session.market_open), _parse(cfg.session.no_new_trades_after)
    flatten_t = _parse(cfg.session.flatten_time)
    time_stop = timedelta(minutes=cfg.risk["limits"]["time_stop_minutes"])
    strike_offset = cfg.execution.option.get("strike_offset", 0)
    slip = cfg.risk["commissions"]["slippage_frac"]
    commission = cfg.risk["commissions"]["per_contract"]
    atr_bars = cfg.risk["targets"]["option_atr_bars"]

    highs, lows, closes, vols = (bars["high"].to_numpy(), bars["low"].to_numpy(),
                                 bars["close"].to_numpy(), bars["volume"].to_numpy())

    i, n = 0, len(bars)
    contracts_fetched = 0
    while i < n:
        now = local[i].to_pydatetime()
        t = now.time()
        if not (open_t <= t < no_new_t):
            i += 1
            continue

        frow = features.iloc[i]
        price = float(closes[i])
        snap = MarketSnapshot(
            timestamp=idx_utc[i].to_pydatetime(), spy_price=price, spy_volume=float(vols[i]),
            vwap=price * (1 + float(frow["vwap_dev"])), atr_5min=float(frow["atr_5"]),
            rvol=float(frow["rvol"]),
            # Prior 5 bars (exclude the current bar) so a breakout of the range is meaningful.
            high_5min=float(highs[max(i - 5, 0):i].max()) if i else float(highs[i]),
            low_5min=float(lows[max(i - 5, 0):i].min()) if i else float(lows[i]),
            vix=float(frow.get("vix", 0.0)) if include_vix else float("nan"),
            regime=classify_regime(frow), ml_prob_up=float(probs[i]), features=frow.to_dict(),
        )

        decision = siggen.generate(snap)
        if decision.signal == Signal.NO_TRADE:
            i += 1
            continue
        ok, _ = pm.can_open(now, equity, 0)
        if not ok:
            i += 1
            continue

        # --- resolve the REAL 0DTE contract and its real bars for the day ---
        right = "C" if decision.signal == Signal.BUY_CALL else "P"
        expiry = now.date()
        contract = poly.nearest_contract(expiry, right, price, strike_offset)
        if contract is None:
            i += 1
            continue
        obars = poly.option_bars(contract["ticker"], expiry)
        contracts_fetched += 1
        entry_ts = idx_utc[i]
        fwd = obars.loc[obars.index >= entry_ts]
        if fwd.empty:
            i += 1
            continue

        entry_bar_ts = fwd.index[0]
        entry_raw = float(fwd.iloc[0]["close"])
        atr = _option_atr(obars, entry_bar_ts, atr_bars)
        entry_px = round(entry_raw * (1 + slip), 2)  # buy: pay up

        intent = pm.build_intent(decision.signal, snap, equity, contract["ticker"],
                                 contract["strike"], entry_px, atr)
        if intent is None:
            i += 1
            continue
        pm.on_open()

        # --- walk real option bars to the exit ---
        day_flatten = pd.Timestamp(datetime.combine(expiry, flatten_t), tz=ET).tz_convert("UTC")
        deadline = min(entry_bar_ts + time_stop, day_flatten)
        walk = obars.loc[(obars.index > entry_bar_ts) & (obars.index <= deadline)]

        exit_raw, exit_reason, exit_ts = None, None, deadline
        for ots, ob in walk.iterrows():
            if float(ob["low"]) <= intent.stop_loss:      # check stop first (pessimistic)
                exit_raw, exit_reason, exit_ts = intent.stop_loss, ExitReason.STOP_LOSS, ots
                break
            if float(ob["high"]) >= intent.take_profit:
                exit_raw, exit_reason, exit_ts = intent.take_profit, ExitReason.TAKE_PROFIT, ots
                break
        if exit_raw is None:
            tail = obars.loc[obars.index <= deadline]
            exit_raw = float(tail.iloc[-1]["close"]) if not tail.empty else entry_raw
            exit_reason = (ExitReason.FLATTEN if deadline == day_flatten else ExitReason.TIME_STOP)
            exit_ts = tail.index[-1] if not tail.empty else entry_bar_ts

        exit_px = round(max(exit_raw * (1 - slip), 0.0), 2)  # sell: give up
        result = TradeResult(
            open_time=entry_bar_ts.to_pydatetime(), close_time=exit_ts.to_pydatetime(),
            right=OptionRight(right), strike=contract["strike"], quantity=intent.quantity,
            entry_price=entry_px, exit_price=exit_px, exit_reason=exit_reason,
            underlying_at_entry=price, underlying_at_exit=price,
            commission=commission * intent.quantity * 2,
        )
        monitor.update(result)
        equity += result.pnl
        pm.record_result(result.pnl)

        # advance past the hold. Use the index's own (unit-aware) searchsorted — a raw
        # int64 view breaks when parquet stores the index as microseconds, not nanoseconds.
        j = int(idx_utc.searchsorted(exit_ts, side="right"))
        i = max(j, i + 1)

    report = summarize(monitor.trades)
    out = {
        "report": report.as_dict(), "final_equity": equity,
        "starting_equity": float(cfg.risk["account"]["starting_equity"]),
        "n_bars": int(n), "contracts_fetched": contracts_fetched,
        "period": f"{bars.index[0]} -> {bars.index[-1]}", "vix": include_vix,
    }
    if verbose:
        print("\n=== Backtest (real Polygon option fills) ===")
        print(out["period"], f"| vix={'yes' if include_vix else 'no'}")
        print(report.pretty())
        print(f"equity: {out['starting_equity']:.2f} -> {out['final_equity']:.2f}")
        print("(Real historical option prices; fills include slippage + commission.)")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest on real Polygon option data")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    cfg = load_config()
    run_backtest(cfg, days=args.days)


if __name__ == "__main__":
    main()
