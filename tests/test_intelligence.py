"""Tests for the intelligence layer: range labels/model, dynamic strikes, defense exits,
liquidity gate, event guard."""
from __future__ import annotations

import numpy as np
import pytest

from src.execution.risk import defense_triggered, liquidity_ok, spread_ev
from src.signals.feature_engineering import build_features
from src.signals.labeling import make_breach_labels, make_range_labels
from src.signals.range_model import RangeForecaster, atr_range_estimate, dynamic_short_otm
from src.utils.events import EventGuard


# --- range labels ---------------------------------------------------------------
def test_range_labels_measure_max_excursion(synthetic_bars):
    y, valid = make_range_labels(synthetic_bars["high"], synthetic_bars["low"],
                                 synthetic_bars["close"], horizon_bars=30)
    assert (y[valid] >= 0).all()
    assert valid.iloc[:-31].all() and not valid.iloc[-1]
    # A known spike must be captured: max excursion >= |window high - entry|/entry.
    i = 100
    entry = synthetic_bars["close"].iloc[i]
    hw = synthetic_bars["high"].iloc[i + 1:i + 31].max()
    lw = synthetic_bars["low"].iloc[i + 1:i + 31].min()
    expected = max(hw - entry, entry - lw) / entry
    assert abs(y.iloc[i] - expected) < 1e-12


# --- range model ----------------------------------------------------------------
def test_range_forecaster_train_save_load(tmp_path, cfg, synthetic_bars):
    feats = build_features(synthetic_bars)
    y, valid = make_range_labels(synthetic_bars["high"], synthetic_bars["low"],
                                 synthetic_bars["close"], horizon_bars=20)
    X, yv = feats[valid.to_numpy()], y[valid.to_numpy()]
    rf = RangeForecaster(params={"num_leaves": 7}, feature_columns=list(X.columns))
    rf.train(X, yv, valid_fraction=0.2, num_boost_round=50, early_stopping_rounds=10)
    preds = rf.predict(X)
    assert (preds >= 0).all()

    mp, mm = tmp_path / "r.txt", tmp_path / "r.json"
    rf.save(mp, mm)
    rf2 = RangeForecaster.load(mp, mm)
    one = rf2.predict_one(feats.iloc[50].to_dict())
    assert one >= 0


# --- strike placement -----------------------------------------------------------
def test_dynamic_short_otm_floors_scales_and_caps():
    # Floors at the static base when the forecast is small.
    assert dynamic_short_otm(0.002, 0.0005, 1.25, 0.01) == 0.002
    # Scales with the forecast when it dominates.
    assert abs(dynamic_short_otm(0.002, 0.004, 1.25, 0.01) - 0.005) < 1e-12
    # Returns None (skip) when no safe strike exists under the cap.
    assert dynamic_short_otm(0.002, 0.02, 1.25, 0.01) is None


def test_atr_range_estimate_scales_with_sqrt_time():
    r60 = atr_range_estimate(0.5, 500.0, 60)
    r15 = atr_range_estimate(0.5, 500.0, 15)
    assert abs(r60 / r15 - 2.0) < 1e-9  # sqrt(60/15) = 2


# --- defensive exit -------------------------------------------------------------
def test_defense_triggered_directions():
    # bull_put: short put at 740; danger when spot falls to/below the buffer above it.
    assert defense_triggered("bull_put", 740.5, 740.0, 0.001)       # within 0.1%
    assert not defense_triggered("bull_put", 745.0, 740.0, 0.001)   # far above: safe
    # bear_call: short call at 750; danger when spot rises near it.
    assert defense_triggered("bear_call", 749.5, 750.0, 0.001)
    assert not defense_triggered("bear_call", 745.0, 750.0, 0.001)
    assert not defense_triggered("unknown", 740.0, 740.0, 0.001)


# --- liquidity gate -------------------------------------------------------------
def test_liquidity_ok_gate():
    # Tight legs: half-spreads sum = (0.02+0.02)/2 = 0.02 <= 25% of 0.30 credit.
    assert liquidity_ok(1.00, 1.02, 0.70, 0.72, credit=0.30, max_frac=0.25)
    # Wide legs: (0.20+0.20)/2 = 0.20 > 0.075 -> reject.
    assert not liquidity_ok(1.00, 1.20, 0.70, 0.90, credit=0.30, max_frac=0.25)
    # NaN / missing -> cannot assess -> reject.
    assert not liquidity_ok(float("nan"), 1.02, 0.70, 0.72, 0.30, 0.25)
    assert not liquidity_ok(None, 1.02, 0.70, 0.72, 0.30, 0.25)
    assert not liquidity_ok(1.00, 1.02, 0.70, 0.72, credit=0.0, max_frac=0.25)


# --- session-boundary label masking (audit C1) -------------------------------------
def _two_day_bars():
    import pandas as pd
    idx1 = pd.date_range("2026-06-01 13:30", periods=390, freq="1min", tz="UTC")
    idx2 = pd.date_range("2026-06-02 13:30", periods=390, freq="1min", tz="UTC")
    idx = idx1.append(idx2)
    rng = np.random.default_rng(7)
    close = 500 * np.cumprod(1 + rng.normal(0, 0.0005, len(idx)))
    high = close * 1.0004
    low = close * 0.9996
    import pandas as pd
    return pd.DataFrame({"high": high, "low": low, "close": close}, index=idx)


def test_labels_never_cross_session_boundary():
    from src.signals.labeling import make_breach_labels, make_labels, make_range_labels
    df = _two_day_bars()
    H = 60
    _, valid_r = make_range_labels(df["high"], df["low"], df["close"], horizon_bars=H)
    _, _, valid_b = make_breach_labels(df["high"], df["low"], df["close"], horizon_bars=H)
    _, valid_d = make_labels(df["close"], horizon_bars=H)
    for valid in (valid_r, valid_b, valid_d):
        # Day 1: the last H rows would peek into day 2 -> must be invalid.
        assert not valid.iloc[390 - H:390].any()
        # Day 1 interior rows remain valid.
        assert valid.iloc[:390 - H].all()
        # Day 2 (final day): last H rows invalid as before (no full window).
        assert not valid.iloc[-H:].any()


# --- cache completeness guard (audit C2) --------------------------------------------
def test_day_is_complete_and_cache_fresh(tmp_path):
    from datetime import date, datetime, timedelta
    from src.data.polygon_options import _cache_fresh, _day_is_complete, _now_et
    yesterday = (_now_et() - timedelta(days=3)).date()
    assert _day_is_complete(yesterday)                    # past day: always complete
    assert _day_is_complete(date(2026, 7, 4))             # Saturday: no session
    # A file written NOW for a range ending 3 days ago is fresh (post-close write).
    f = tmp_path / "x.parquet"
    f.write_text("stub")
    assert _cache_fresh(f, yesterday)
    assert not _cache_fresh(tmp_path / "missing.parquet", yesterday)


# --- breach labels + EV gate ------------------------------------------------------
def test_breach_labels_directional(synthetic_bars):
    dn, up, valid = make_breach_labels(synthetic_bars["high"], synthetic_bars["low"],
                                       synthetic_bars["close"], horizon_bars=30,
                                       threshold_pct=0.002)
    assert set(dn[valid].unique()) <= {0, 1} and set(up[valid].unique()) <= {0, 1}
    assert not valid.iloc[-1]
    # Consistency with the range label: any breach implies max excursion >= threshold.
    rng, rvalid = make_range_labels(synthetic_bars["high"], synthetic_bars["low"],
                                    synthetic_bars["close"], horizon_bars=30)
    both = valid & rvalid
    breached = (dn[both] == 1) | (up[both] == 1)
    assert (rng[both][breached] >= 0.002 - 1e-12).all()


def test_spread_ev_exit_structure_math():
    # pt=0.5, stop=2x: breakeven at P(breach)=1/3.
    assert abs(spread_ev(0.30, 1 / 3, 0.5, 2.0)) < 1e-12
    assert spread_ev(0.30, 0.20, 0.5, 2.0) > 0        # cheap risk -> positive EV
    assert spread_ev(0.30, 0.50, 0.5, 2.0) < 0        # rich risk -> negative EV
    # No breach at all -> full profit-target expectation.
    assert abs(spread_ev(0.30, 0.0, 0.5, 2.0) - 0.15) < 1e-12
    # Probability clamped to [0, 1].
    assert spread_ev(0.30, -1.0, 0.5, 2.0) == spread_ev(0.30, 0.0, 0.5, 2.0)
    assert spread_ev(0.30, 2.0, 0.5, 2.0) == spread_ev(0.30, 1.0, 0.5, 2.0)


# --- event guard ----------------------------------------------------------------
def test_event_guard_block_and_widen(tmp_path):
    f = tmp_path / "events.yaml"
    f.write_text("events:\n  - date: 2026-07-29\n    name: FOMC\n    action: block\n"
                 "  - date: 2026-07-14\n    name: CPI\n    action: widen\n")
    from datetime import date
    g = EventGuard(f)
    assert g.check(date(2026, 7, 29))["action"] == "block"
    assert g.check(date(2026, 7, 14))["action"] == "widen"
    assert g.check(date(2026, 7, 15)) is None


# --- gap guard + experiment arms ---------------------------------------------------
def test_gap_exceeds():
    from src.execution.risk import gap_exceeds
    assert gap_exceeds(0.015, 0.01)          # +1.5% gap blocks at 1% threshold
    assert gap_exceeds(-0.02, 0.01)          # down-gaps count too
    assert not gap_exceeds(0.005, 0.01)
    assert not gap_exceeds(None, 0.01)       # unknown gap never blocks


def test_assign_arm_deterministic_and_balanced():
    from src.execution.risk import assign_arm
    arms = [5, 10]
    a = assign_arm("2026-07-06T10:31", arms)
    assert a == assign_arm("2026-07-06T10:31", arms)     # deterministic
    assert a in arms
    # Over many minutes the split should be roughly balanced (no degenerate constant).
    picks = [assign_arm(f"2026-07-06T{h:02d}:{m:02d}", arms)
             for h in range(10, 15) for m in range(0, 60)]
    frac10 = sum(1 for p in picks if p == 10) / len(picks)
    assert 0.35 < frac10 < 0.65


# --- retrain sanity floor -----------------------------------------------------------
def test_retrain_sanity_floor(tmp_path, monkeypatch):
    from src.learning import trainer
    monkeypatch.setattr(trainer, "_METRICS_HISTORY", tmp_path / "hist.json")
    # Insufficient history: everything passes.
    assert trainer._sanity_ok("m", 99.0)
    for v in (0.30, 0.31, 0.29, 0.30):
        trainer._record_metric("m", v)
    assert trainer._sanity_ok("m", 0.35)      # near median: fine
    assert not trainer._sanity_ok("m", 0.90)  # 3x median: corrupted-data signature


# --- trade log --------------------------------------------------------------------
def test_trade_log_roundtrip_and_slippage(tmp_path):
    from src.utils.trade_log import TradeLog
    tl = TradeLog(tmp_path / "t.db")
    tid = tl.open_trade(opened_at="2026-07-06T10:00:00", kind="bull_put",
                        short_strike=743.0, long_strike=738.0, width=5.0, quantity=1,
                        credit_est=0.40, spot=744.5, regime="chop", ml_prob=0.56,
                        range_pred=0.0031, p_breach_dn=0.42, p_breach_up=0.38,
                        iv_short=0.19, rv_annual=0.14, rv_60m=0.15, rvol=1.4, atr_5=0.35,
                        minutes_into_session=45.0)
    tl.close_trade(tid, closed_at="2026-07-06T11:30:00", exit_reason="take_profit",
                   exit_cost_est=0.20, exit_cost_fill=0.22, credit_fill=0.38,
                   pnl=13.40, limit_exit=True)
    assert tl.count() == 1
    rep = tl.report()
    assert "n=1" in rep and "noise" in rep          # honest small-sample warning
    assert "IV>RV entries" in rep                    # 0.19 > 0.14 bucket populated
    row = tl._conn.execute("SELECT * FROM trades").fetchone()
    assert abs(row["entry_slippage"] - 0.02) < 1e-9  # est 0.40 vs fill 0.38
    assert abs(row["exit_slippage"] - 0.02) < 1e-9   # fill 0.22 vs est 0.20
    tl.close()


def test_consecutive_loss_brake(cfg):
    from datetime import datetime
    from src.execution.position_manager import PositionManager
    pm = PositionManager(cfg)
    now = datetime(2026, 7, 6, 10, 0)
    limit = cfg.risk["limits"]["max_consecutive_losses"]
    for _ in range(limit):
        pm.record_result(-10.0)
    ok, why = pm.can_open(now, 100000, 0)            # equity high: daily halt not the cause
    assert not ok and why == "consecutive_loss_brake"
    pm.record_result(+5.0)                            # a win resets the brake
    ok, _ = pm.can_open(now, 100000, 0)
    assert ok


def test_event_guard_missing_and_malformed(tmp_path):
    from datetime import date
    assert EventGuard(tmp_path / "nope.yaml").check(date(2026, 1, 1)) is None
    bad = tmp_path / "bad.yaml"
    bad.write_text("events: {not: [valid")
    assert EventGuard(bad).check(date(2026, 1, 1)) is None  # disabled, not crashed