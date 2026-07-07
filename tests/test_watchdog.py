"""Test the trading-loop watchdog's stall predicate (2026-07-07 hang incident)."""
from __future__ import annotations

from src.main import _loop_stalled


def test_loop_stalled_predicate():
    assert not _loop_stalled(1000.0, 1100.0, 150.0)   # 100s elapsed < 150s threshold
    assert _loop_stalled(1000.0, 1200.0, 150.0)        # 200s elapsed > 150s -> stalled
    assert not _loop_stalled(1000.0, 1000.0, 150.0)    # no time passed
    assert _loop_stalled(0.0, 151.0, 150.0)            # just over the line
