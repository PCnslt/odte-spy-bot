"""Regression for the 2026-07-08 fill/P&L bug: close_credit_spread must NEVER fire an order
for an entry that didn't fill (that created phantom short positions), and must unwind only the
quantity that actually filled."""
from __future__ import annotations

from src.execution.ibkr_broker import IBKRBroker


class _Status:
    def __init__(self, status, filled):
        self.status, self.filled = status, filled


class _Trade:
    def __init__(self, status, filled):
        self.orderStatus = _Status(status, filled)
        self.order = object()


class _FakeIB:
    def __init__(self):
        self.placed = []

    def cancelOrder(self, o):
        pass

    def sleep(self, *a):
        pass

    def placeOrder(self, combo, order):
        self.placed.append(order)
        return "CLOSE_TRADE"


def _broker(cfg):
    b = IBKRBroker(cfg, mode="paper")
    b.ib = _FakeIB()
    return b


def _pos(status, filled, qty=5):
    return {"trade": _Trade(status, filled), "order": object(), "combo": object(),
            "quantity": qty}


def test_unfilled_pending_entry_places_no_order(cfg):
    b = _broker(cfg)
    res = b.close_credit_spread(_pos("PendingSubmit", 0))   # the exact 2026-07-08 case
    assert res is None
    assert b.ib.placed == []                                 # NO phantom market SELL


def test_filled_entry_closes_full_qty(cfg):
    b = _broker(cfg)
    res = b.close_credit_spread(_pos("Filled", 5), limit_cost=0.20)
    assert res == "CLOSE_TRADE" and len(b.ib.placed) == 1
    assert b.ib.placed[0].action == "SELL" and b.ib.placed[0].totalQuantity == 5


def test_partial_fill_closes_only_filled(cfg):
    b = _broker(cfg)
    b.close_credit_spread(_pos("Submitted", 2, qty=5))       # only 2 of 5 filled
    assert len(b.ib.placed) == 1 and b.ib.placed[0].totalQuantity == 2


class _CloseTrade:
    def __init__(self, status, remaining):
        self.orderStatus = _Status(status, 0)
        self.orderStatus.remaining = remaining
        self.order = object()


def test_escalate_sells_only_the_unfilled_remainder(cfg):
    """A limit close that partially filled (2 of 5) must escalate the REMAINING 3 to market,
    never the full 5 — else it over-sells into a phantom short (2026-07-08 incident class)."""
    b = _broker(cfg)
    pos = {"close_trade": _CloseTrade("Submitted", 3), "combo": object(), "quantity": 5}
    b.escalate_close(pos)
    assert len(b.ib.placed) == 1
    assert b.ib.placed[0].action == "SELL" and b.ib.placed[0].totalQuantity == 3


def test_escalate_is_noop_when_limit_fully_filled(cfg):
    b = _broker(cfg)
    ct = _CloseTrade("Submitted", 0)                          # remaining 0 => nothing left
    res = b.escalate_close({"close_trade": ct, "combo": object(), "quantity": 5})
    assert b.ib.placed == [] and res is ct
