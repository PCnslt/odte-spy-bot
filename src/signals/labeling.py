"""Label construction for supervised training. STRICTLY CAUSAL AND SESSION-BOUNDED.

Session-boundary rule (audit finding C1): every forward window must lie entirely within the
same ET trading date as its row. Without this, rows near the close are labeled by overnight
gaps and next-session moves that the live system (which flattens at 15:55 and trades 0DTE)
can never capture — a structural leakage that inflates apparent model skill. All three label
functions enforce it via `_same_session_mask`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ET = "America/New_York"


def _same_session_mask(index: pd.Index, horizon_bars: int) -> np.ndarray:
    """mask[i] is True iff bar i+horizon exists AND falls on the same ET calendar date as
    bar i — i.e., the forward window never crosses an overnight boundary."""
    n = len(index)
    mask = np.zeros(n, dtype=bool)
    if isinstance(index, pd.DatetimeIndex):
        idx = index.tz_convert(ET) if index.tz is not None else index
        dates = np.asarray(idx.date)
    else:  # non-datetime index (synthetic tests): no boundary information, allow all
        dates = None
    for i in range(n - horizon_bars):
        mask[i] = dates is None or dates[i] == dates[i + horizon_bars]
    return mask


def make_labels(close: pd.Series, horizon_bars: int = 5,
                threshold_pct: float = 0.0015) -> tuple[pd.Series, pd.Series]:
    """Directional triple-barrier-lite label. Returns (labels, valid_mask)."""
    n = len(close)
    values = close.to_numpy()
    labels = np.zeros(n, dtype=np.int8)
    valid = _same_session_mask(close.index, horizon_bars)

    up = 1.0 + threshold_pct
    dn = 1.0 - threshold_pct

    for i in range(n - horizon_bars):
        if not valid[i]:
            continue
        entry = values[i]
        window = values[i + 1 : i + 1 + horizon_bars]
        hit_up = np.argmax(window >= entry * up) if np.any(window >= entry * up) else None
        hit_dn = np.argmax(window <= entry * dn) if np.any(window <= entry * dn) else None
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
    Returns (breach_dn, breach_up, valid_mask). Windows never cross the session boundary.
    """
    n = len(close)
    c = close.to_numpy()
    h = high.to_numpy()
    l = low.to_numpy()
    dn = np.zeros(n, dtype=np.int8)
    up = np.zeros(n, dtype=np.int8)
    valid = _same_session_mask(close.index, horizon_bars)
    for i in range(n - horizon_bars):
        if not valid[i]:
            continue
        lw = l[i + 1: i + 1 + horizon_bars].min()
        hw = h[i + 1: i + 1 + horizon_bars].max()
        dn[i] = 1 if (c[i] - lw) / c[i] >= threshold_pct else 0
        up[i] = 1 if (hw - c[i]) / c[i] >= threshold_pct else 0
    return (pd.Series(dn, index=close.index, name="breach_dn"),
            pd.Series(up, index=close.index, name="breach_up"),
            pd.Series(valid, index=close.index, name="valid"))


def make_range_labels(high: pd.Series, low: pd.Series, close: pd.Series,
                      horizon_bars: int = 60) -> tuple[pd.Series, pd.Series]:
    """Forward MAX EXCURSION labels for range forecasting (the spread-seller's target).

    For each bar t: the largest one-sided move over the next `horizon_bars` bars,
        range_t = max(max(high[t+1..t+H]) - close_t, close_t - min(low[t+1..t+H])) / close_t
    A credit-spread seller cares whether price can REACH the short strike — this is that
    distance, directly. Returns (range_frac, valid_mask). Windows never cross the session
    boundary (an overnight gap is not an intraday range the live system can trade).
    """
    n = len(close)
    c = close.to_numpy()
    h = high.to_numpy()
    l = low.to_numpy()
    out = np.zeros(n)
    valid = _same_session_mask(close.index, horizon_bars)
    for i in range(n - horizon_bars):
        if not valid[i]:
            continue
        hw = h[i + 1: i + 1 + horizon_bars].max()
        lw = l[i + 1: i + 1 + horizon_bars].min()
        out[i] = max(hw - c[i], c[i] - lw) / c[i]
    return (pd.Series(out, index=close.index, name="range_frac"),
            pd.Series(valid, index=close.index, name="valid"))
