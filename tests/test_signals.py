from __future__ import annotations

import numpy as np

from src.common import Signal
from src.signals.feature_engineering import FEATURE_COLUMNS, build_features
from src.signals.labeling import make_labels
from src.signals.regime_classifier import classify_regime
from src.signals.signal_generator import SignalGenerator


def test_features_have_all_columns_and_no_nans(synthetic_bars):
    feats = build_features(synthetic_bars)
    assert list(feats.columns) == FEATURE_COLUMNS
    assert len(feats) == len(synthetic_bars)
    assert not feats.isna().any().any()
    assert np.isfinite(feats.to_numpy()).all()


def test_features_are_causal(synthetic_bars):
    # Truncating future rows must not change past feature values.
    full = build_features(synthetic_bars)
    half = build_features(synthetic_bars.iloc[:200])
    # Compare an interior row well past warmup.
    np.testing.assert_allclose(
        full.iloc[150].to_numpy(), half.iloc[150].to_numpy(), rtol=1e-9, atol=1e-9
    )


def test_labels_binary_and_masked(synthetic_bars):
    labels, valid = make_labels(synthetic_bars["close"], horizon_bars=5, threshold_pct=0.0015)
    assert set(np.unique(labels)) <= {0, 1}
    # Last `horizon` rows can't have a full forward window.
    assert not valid.iloc[-1]


def test_regime_returns_enum(synthetic_bars):
    feats = build_features(synthetic_bars)
    r = classify_regime(feats.iloc[100])
    assert r.value in {"trend_up", "trend_down", "chop", "volatile"}


def test_signal_generator_no_trade_on_low_rvol(cfg, synthetic_bars):
    from src.common import MarketSnapshot
    gen = SignalGenerator(cfg)
    snap = MarketSnapshot(
        timestamp=synthetic_bars.index[100].to_pydatetime(),
        spy_price=500.0, vwap=499.0, rvol=0.5, high_5min=500.0, low_5min=498.0,
        ml_prob_up=0.9,
    )
    assert gen.generate(snap).signal == Signal.NO_TRADE
