"""Transparent market-regime tag. No black box: pure thresholds on ATR, EMA slope, and VIX.

Regimes: TREND_UP, TREND_DOWN, CHOP, VOLATILE. VOLATILE takes precedence because it changes
how much conviction the signal layer demands before trading.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..common import Regime


def classify_regime(features_row: pd.Series, vix_high: float = 22.0,
                    atr_high_pct: float = 0.0015, slope_eps: float = 0.0002) -> Regime:
    """Classify a single bar from its feature row."""
    vix = float(features_row.get("vix", 0.0)) or 0.0
    atr5 = float(features_row.get("atr_5", 0.0))
    rv = float(features_row.get("rv_5", 0.0))
    ema_slope = float(features_row.get("ema_slope", 0.0))
    ema_spread = float(features_row.get("ema_9_21_spread", 0.0))

    # Volatile if VIX elevated or realized vol spikes.
    if vix >= vix_high or rv >= atr_high_pct * 2:
        return Regime.VOLATILE

    if ema_slope > slope_eps and ema_spread > 0:
        return Regime.TREND_UP
    if ema_slope < -slope_eps and ema_spread < 0:
        return Regime.TREND_DOWN
    return Regime.CHOP


def classify_series(features: pd.DataFrame, **kwargs) -> pd.Series:
    return features.apply(lambda r: classify_regime(r, **kwargs), axis=1)
