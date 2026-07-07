"""Tests for the holdout-integrity guard (numpy-free; pure logic + a json ledger)."""
from __future__ import annotations

from datetime import date

import pytest

from src.utils import holdout
from src.utils.holdout import (HOLDOUT_END, HOLDOUT_START, HoldoutViolation, guard,
                               intersects_holdout, ledger_status)


def test_intersects_holdout_boundaries():
    assert not intersects_holdout(date(2024, 1, 1), date(2024, 12, 31))   # fully before
    assert not intersects_holdout(date(2025, 7, 1), date(2026, 3, 1))     # fully after
    assert intersects_holdout(date(2025, 1, 2), date(2025, 6, 30))        # exact holdout
    assert intersects_holdout(date(2024, 12, 1), date(2025, 2, 1))        # straddles start
    assert intersects_holdout(date(2025, 6, 1), date(2025, 8, 1))         # straddles end
    assert intersects_holdout(HOLDOUT_END, HOLDOUT_END)                   # single day inside
    assert not intersects_holdout(date(2025, 7, 1), date(2025, 7, 2))     # day after


def test_guard_noop_off_holdout(tmp_path):
    # A normal recent range never touches the holdout -> no raise, no ledger written.
    guard(date(2026, 1, 1), date(2026, 7, 1), ledger_path=tmp_path / "l.json", env={})
    assert not (tmp_path / "l.json").exists()


def test_guard_fails_closed_without_token(tmp_path):
    with pytest.raises(HoldoutViolation):
        guard(date(2025, 3, 1), date(2025, 4, 1), ledger_path=tmp_path / "l.json", env={})


def test_guard_allows_one_look_then_refuses(tmp_path):
    led = tmp_path / "l.json"
    # First H5 look into the holdout: allowed + recorded.
    guard(HOLDOUT_START, HOLDOUT_END, ledger_path=led, env={"ODTE_CONFIRM": "H5"})
    assert "H5" in ledger_status(led)["consumed_looks"]
    # Second H5 look: refused — one look per hypothesis.
    with pytest.raises(HoldoutViolation):
        guard(HOLDOUT_START, HOLDOUT_END, ledger_path=led, env={"ODTE_CONFIRM": "H5"})
    # A different, unused hypothesis is still allowed.
    guard(HOLDOUT_START, HOLDOUT_END, ledger_path=led, env={"ODTE_CONFIRM": "H2"})
    assert ledger_status(led)["n_consumed"] == 2


def test_token_not_consumed_when_range_off_holdout(tmp_path):
    led = tmp_path / "l.json"
    # Token present but range doesn't touch the holdout -> no consumption.
    guard(date(2026, 1, 1), date(2026, 2, 1), ledger_path=led, env={"ODTE_CONFIRM": "H5"})
    assert not led.exists()


def test_load_bars_wires_the_guard(monkeypatch):
    """Sanity: load_bars calls the guard with the computed range BEFORE fetching any data."""
    from src.data import data_pipeline
    assert data_pipeline.holdout_guard is guard          # wired at import time
    called = {}

    class _Stop(Exception):
        pass

    def fake_guard(s, e):
        called["range"] = (s, e)
        raise _Stop                                       # halt right at the guard
    monkeypatch.setattr(data_pipeline, "holdout_guard", fake_guard)
    monkeypatch.setattr(data_pipeline, "_date_range",
                        lambda days: (date(2025, 3, 1), date(2025, 4, 1)))
    with pytest.raises(_Stop):
        data_pipeline.load_bars(cfg=None, days=400, download=True, poly=object())
    assert called["range"] == (date(2025, 3, 1), date(2025, 4, 1))
