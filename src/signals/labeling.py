"""Directional labels for supervised training.

Triple-barrier-lite: for each bar, look forward `horizon` bars. Label 1 ("up") if the close
rises by >= +threshold before falling by <= -threshold within the horizon; label 0 otherwise.
Bars without a full forward window are dropped (returned mask marks valid rows).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def make_labels(close: pd.Series, horizon_bars: int = 5,
                threshold_pct: float = 0.0015) -> tuple[pd.Series, pd.Series]:
    """Return (labels, valid_mask), both aligned to `close.index`."""
    n = len(close)
    values = close.to_numpy()
    labels = np.zeros(n, dtype=np.int8)
    valid = np.zeros(n, dtype=bool)

    up = 1.0 + threshold_pct
    dn = 1.0 - threshold_pct

    for i in range(n - horizon_bars):
        entry = values[i]
        window = values[i + 1 : i + 1 + horizon_bars]
        hit_up = np.argmax(window >= entry * up) if np.any(window >= entry * up) else None
        hit_dn = np.argmax(window <= entry * dn) if np.any(window <= entry * dn) else None
        valid[i] = True
        if hit_up is None and hit_dn is None:
            # No barrier touched: label by sign of terminal return.
            labels[i] = 1 if window[-1] > entry else 0
        elif hit_up is not None and (hit_dn is None or hit_up <= hit_dn):
            labels[i] = 1
        else:
            labels[i] = 0

    return (pd.Series(labels, index=close.index, name="label"),
            pd.Series(valid, index=close.index, name="valid"))


def make_breach_labels(high: pd.Series, low: pd.Series, close: pd.Series,
                       horizon_bars: int = 120,
                       threshold_pct: float = 0.002) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Directional BREACH labels for the premium-richness (EV) gate.

    breach_dn[t] = 1 if price falls >= threshold_pct below close[t] within the horizon
                   (the bull-put seller's risk event: spot reaching the short put).
    breach_up[t] = 1 if price rises >= threshold_pct above close[t] within the horizon
                   (the bear-call seller's risk event).
    Returns (breach_dn, breach_up, valid_mask).
    """
    n = len(close)
    c = close.to_numpy()
    h = high.to_numpy()
    l = low.to_numpy()
    dn = np.zeros(n, dtype=np.int8)
    up = np.zeros(n, dtype=np.int8)
    valid = np.zeros(n, dtype=bool)
    for i in range(n - horizon_bars):
        lw = l[i + 1: i + 1 + horizon_bars].min()
        hw = h[i + 1: i + 1 + horizon_bars].max()
        dn[i] = 1 if (c[i] - lw) / c[i] >= threshold_pct else 0
        up[i] = 1 if (hw - c[i]) / c[i] >= threshold_pct else 0
        valid[i] = True
    return (pd.Series(dn, index=close.index, name="breach_dn"),
            pd.Series(up, index=close.index, name="breach_up"),
            pd.Series(valid, index=close.index, name="valid"))


def make_range_labels(high: pd.Series, low: pd.Series, close: pd.Series,
                      horizon_bars: int = 60) -> tuple[pd.Series, pd.Series]:
    """Forward MAX EXCURSION labels for range forecasting (the spread-seller's target).

    For each bar t: the largest one-sided move over the next `horizon_bars` bars,
        range_t = max(max(high[t+1..t+H]) - close_t, close_t - min(low[t+1..t+H])) / close_t
    A credit-spread seller cares whether price can REACH the short strike — this is that
    distance, directly. Returns (range_frac, valid_mask) aligned to close.index.
    """
    n = len(close)
    c = close.to_numpy()
    h = high.to_numpy()
    l = low.to_numpy()
    out = np.zeros(n)
    valid = np.zeros(n, dtype=bool)
    for i in range(n - horizon_bars):
        hw = h[i + 1: i + 1 + horizon_bars].max()
        lw = l[i + 1: i + 1 + horizon_bars].min()
        out[i] = max(hw - c[i], c[i] - lw) / c[i]
        valid[i] = True
    return (pd.Series(out, index=close.index, name="range_frac"),
            pd.Series(valid, index=close.index, name="valid"))
