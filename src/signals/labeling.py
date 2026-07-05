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
