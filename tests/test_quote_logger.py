"""Quote-logger pure functions: strike windows and the never-fabricate row rule."""
from __future__ import annotations

from src.research.quote_logger import quote_row, strike_window


def test_strike_window_brackets_spot():
    ks = strike_window(75.0, pct=0.02, step=1.0)
    assert ks[0] <= 75.0 * 0.98 and ks[-1] >= 75.0 * 1.02
    assert all(b - a == 1.0 for a, b in zip(ks, ks[1:]))     # fixed $1 grid


def test_strike_window_rejects_garbage():
    assert strike_window(0) == []
    assert strike_window(-5) == []
    assert strike_window(75.0, pct=0) == []


def test_quote_row_writes_only_real_two_sided_quotes():
    ok = quote_row("t", "XSP", "20260720", 75.0, "P", 0.40, 0.45, 5, 7)
    assert ok == ["t", "XSP", "20260720", 75.0, "P", 0.40, 0.45, 5, 7]
    assert quote_row("t", "XSP", "e", 75.0, "P", None, 0.45, 1, 1) is None   # one-sided
    assert quote_row("t", "XSP", "e", 75.0, "P", 0.50, 0.45, 1, 1) is None   # crossed
    assert quote_row("t", "XSP", "e", 75.0, "P", float("nan"), 0.45, 1, 1) is None
    assert quote_row("t", "XSP", "e", 75.0, "P", -0.1, 0.05, 1, 1) is None
    assert quote_row("t", "XSP", "e", 75.0, "P", 0.40, 0.45, None, None)[7] == 0
