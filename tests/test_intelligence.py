"""Tests for the intelligence layer: range labels/model, dynamic strikes, defense exits,
liquidity gate, event guard."""
from __future__ import annotations

import numpy as np
import pytest

from src.execution.risk import defense_triggered, liquidity_ok
from src.signals.feature_engineering import build_features
from src.signals.labeling import make_range_labels
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


def test_event_guard_missing_and_malformed(tmp_path):
    from datetime import date
    assert EventGuard(tmp_path / "nope.yaml").check(date(2026, 1, 1)) is None
    bad = tmp_path / "bad.yaml"
    bad.write_text("events: {not: [valid")
    assert EventGuard(bad).check(date(2026, 1, 1)) is None  # disabled, not crashed