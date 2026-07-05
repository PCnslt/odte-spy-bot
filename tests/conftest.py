"""Shared fixtures: a synthetic SPY minute-bar frame and a loaded config."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.utils.config import load_config


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def synthetic_bars():
    """One RTH session of deterministic minute bars with a mild uptrend + noise."""
    idx = pd.date_range("2026-06-01 13:30", periods=390, freq="1min", tz="UTC")  # 09:30 ET
    rng = np.random.default_rng(42)
    steps = rng.normal(0.0002, 0.0009, size=len(idx))
    close = 500 * np.cumprod(1 + steps)
    high = close * (1 + np.abs(rng.normal(0, 0.0004, len(idx))))
    low = close * (1 - np.abs(rng.normal(0, 0.0004, len(idx))))
    open_ = np.concatenate([[close[0]], close[:-1]])
    volume = rng.integers(50_000, 200_000, len(idx)).astype(float)
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume,
         "vix": 16 + rng.normal(0, 0.5, len(idx))},
        index=idx,
    )
    return df
