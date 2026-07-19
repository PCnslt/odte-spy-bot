"""Broker-truth R3 fix: position reads can force a reqPositions round-trip, not just the cache.

ib.positions() returns the local event cache; ib.sleep(0) does NOT round-trip, so a fill TWS
hasn't pushed yet is invisible for a poll. orphan_positions() (which gates NEW entries via the
per-poll sweep) must refresh by default; positions_for() stays cache-only unless asked.
"""
from __future__ import annotations

from src.execution.ibkr_broker import IBKRBroker


class _RefreshIB:
    """Minimal fake IB that records whether a reqPositions round-trip happened."""
    def __init__(self, positions=None):
        self._positions = positions or []
        self.reqpos_calls = 0
        self.waited = 0

    def reqPositions(self):
        self.reqpos_calls += 1

    def waitOnUpdate(self, timeout=None):
        self.waited += 1

    def sleep(self, *a):
        pass

    def positions(self):
        return self._positions


def _broker(cfg, ib=None):
    b = IBKRBroker(cfg, mode="paper")
    b.ib = ib or _RefreshIB()
    return b


def test_positions_for_is_cache_only_by_default(cfg):
    b = _broker(cfg)
    b.positions_for([123])
    assert b.ib.reqpos_calls == 0            # fast path: no round-trip unless asked


def test_positions_for_refresh_round_trips(cfg):
    b = _broker(cfg)
    b.positions_for([123], refresh=True)
    assert b.ib.reqpos_calls == 1 and b.ib.waited == 1


def test_orphan_positions_forces_refresh_by_default(cfg):
    b = _broker(cfg)
    b.orphan_positions()                     # the entry-gating sweep MUST see broker truth
    assert b.ib.reqpos_calls == 1


def test_refresh_is_fail_soft(cfg):
    class _BoomIB(_RefreshIB):
        def reqPositions(self):
            raise RuntimeError("gateway busy")
    b = _broker(cfg, _BoomIB())
    b.positions_for([123], refresh=True)     # must NOT raise — falls back to the cache
    assert b.orphan_positions() == []
