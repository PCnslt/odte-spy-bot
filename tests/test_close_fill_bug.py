"""Order-lifecycle regressions for the incidents of 2026-07-08 / 07-09.

The fakes model the REAL ib_insync contract the broker now depends on:
  * terminal states are Filled/Cancelled/ApiCancelled (`trade.isDone()`), not a status string
  * `trade.filled()` / `trade.remaining()` come from executions
  * a cancel is only observable after the event loop is pumped (`waitOnUpdate`)
`ib.sleep(0)` is a single loop tick with NO network round-trip, so it can never confirm a
cancel — that gap is what produced the phantom shorts.
"""
from __future__ import annotations

from src.execution.ibkr_broker import IBKRBroker

DONE = ("Filled", "Cancelled", "ApiCancelled")


class _Order:
    def __init__(self, trade=None):
        self._trade = trade


class _Status:
    def __init__(self, status, filled):
        self.status, self.filled = status, filled
        self.remaining = None
        self.avgFillPrice = 0.0


class _Trade:
    """Mimics ib_insync Trade."""

    def __init__(self, status, filled, total=5):
        self.orderStatus = _Status(status, filled)
        self._total = total
        self.order = _Order(self)
        self.log = []

    def isDone(self):
        return self.orderStatus.status in DONE

    def filled(self):
        return self.orderStatus.filled

    def remaining(self):
        return self._total - self.orderStatus.filled

    def _settle_cancel(self):
        if self.orderStatus.status not in DONE:
            self.orderStatus.status = "Cancelled"


class _FakeIB:
    def __init__(self):
        self.placed = []
        self._pending_cancels = []

    def cancelOrder(self, order):
        self._pending_cancels.append(order)

    def waitOnUpdate(self, timeout=None):
        """IBKR confirming the cancels — only here does state actually change."""
        for o in self._pending_cancels:
            if getattr(o, "_trade", None) is not None:
                o._trade._settle_cancel()
        self._pending_cancels.clear()

    def sleep(self, *a):
        pass

    def placeOrder(self, contract, order):
        self.placed.append(order)
        return "CLOSE_TRADE"

    def positions(self):
        return []

    def openTrades(self):
        return []


def _broker(cfg):
    b = IBKRBroker(cfg, mode="paper")
    b.ib = _FakeIB()
    return b


def _pos(status, filled, qty=5):
    tr = _Trade(status, filled, total=qty)
    return {"trade": tr, "order": tr.order, "combo": object(), "quantity": qty}


# --- entry unwind ---------------------------------------------------------------------------
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


def test_cancel_is_confirmed_before_reading_fill(cfg):
    """A fill that lands while the cancel is in flight must be SEEN. Before the fix, sleep(0)
    returned without a round-trip, filled read 0, and the live position was dropped."""
    b = _broker(cfg)
    tr = _Trade("Submitted", 0, total=5)
    pos = {"trade": tr, "order": tr.order, "combo": object(), "quantity": 5}

    # IBKR reports a full fill at the moment the cancel is processed.
    def _fill_during_cancel(timeout=None):
        tr.orderStatus.status, tr.orderStatus.filled = "Filled", 5
    b.ib.waitOnUpdate = _fill_during_cancel

    b.close_credit_spread(pos)
    assert len(b.ib.placed) == 1 and b.ib.placed[0].totalQuantity == 5   # unwinds the real 5


# --- escalation -----------------------------------------------------------------------------
def test_escalate_sells_only_the_unfilled_remainder(cfg):
    """Limit close partially filled (2 of 5) → escalate only the REMAINING 3, never the full 5,
    else we over-sell into a phantom short."""
    b = _broker(cfg)
    ct = _Trade("Submitted", 2, total=5)
    b.escalate_close({"close_trade": ct, "combo": object(), "quantity": 5})
    assert len(b.ib.placed) == 1
    assert b.ib.placed[0].action == "SELL" and b.ib.placed[0].totalQuantity == 3


def test_escalate_is_noop_when_limit_fully_filled(cfg):
    b = _broker(cfg)
    ct = _Trade("Submitted", 5, total=5)      # fully filled; remaining 0
    res = b.escalate_close({"close_trade": ct, "combo": object(), "quantity": 5})
    assert b.ib.placed == [] and res is ct


def test_escalate_waits_for_cancel_before_sizing(cfg):
    """If a fill completes during the cancel, the escalation must send NOTHING."""
    b = _broker(cfg)
    ct = _Trade("Submitted", 0, total=5)

    def _fill_during_cancel(timeout=None):
        ct.orderStatus.status, ct.orderStatus.filled = "Filled", 5
    b.ib.waitOnUpdate = _fill_during_cancel

    b.escalate_close({"close_trade": ct, "combo": object(), "quantity": 5})
    assert b.ib.placed == []                   # remaining==0 → no double-sell


# --- orphan sweep ---------------------------------------------------------------------------
class _Position:
    def __init__(self, sec_type, symbol, qty, con_id=1):
        self.contract = type("C", (), {"secType": sec_type, "symbol": symbol,
                                       "localSymbol": f"{symbol}{sec_type}", "exchange": "",
                                       "currency": "USD", "conId": con_id})()
        self.position = qty
        self.avgCost = 1.0


def test_flatten_orphans_sweeps_assigned_shares(cfg):
    """A breached 0DTE short call that expires assigned leaves SHORT stock. The OPT-only sweep
    would have left it naked (2026-07-09)."""
    b = _broker(cfg)
    b.ib.positions = lambda: [_Position("STK", cfg.symbol, -200, 1),
                              _Position("OPT", cfg.symbol, 2, 2)]
    assert b.flatten_orphans() == 2
    placed = [(o.action, o.totalQuantity) for o in b.ib.placed]
    assert ("BUY", 200) in placed        # short stock bought back to flat
    assert ("SELL", 2) in placed         # long option sold to flat


def test_flatten_orphans_ignores_flat_and_other_symbols(cfg):
    b = _broker(cfg)
    b.ib.positions = lambda: [_Position("STK", cfg.symbol, 0, 1),
                              _Position("STK", "QQQ", -100, 2),
                              _Position("OPT", cfg.symbol, 1, 3)]
    assert b.flatten_orphans() == 1


class _WorkingTrade:
    def __init__(self, con_id, remaining):
        self.contract = type("C", (), {"conId": con_id})()
        self.orderStatus = _Status("Submitted", 0)
        self.orderStatus.remaining = remaining
        self.order = type("O", (), {"totalQuantity": remaining})()

    def isDone(self):
        return False


def test_flatten_orphans_never_stacks_orders(cfg):
    """The per-poll sweep runs every 30s. Pre-market a stock MarketOrder is HELD, not filled —
    re-sending would queue N covers that all fill at the open and flip us massively long."""
    b = _broker(cfg)
    b.ib.positions = lambda: [_Position("STK", cfg.symbol, -200, 1)]
    b.ib.openTrades = lambda: [_WorkingTrade(1, 200)]     # a 200-share cover already working
    assert b.flatten_orphans() == 0
    assert b.ib.placed == []                               # nothing re-sent


def test_flatten_orphans_tops_up_a_partial_cover(cfg):
    b = _broker(cfg)
    b.ib.positions = lambda: [_Position("STK", cfg.symbol, -200, 1)]
    b.ib.openTrades = lambda: [_WorkingTrade(1, 50)]       # only 50 working of the 200 needed
    assert b.flatten_orphans() == 1
    assert (b.ib.placed[0].action, b.ib.placed[0].totalQuantity) == ("BUY", 150)


class _WorkingCombo:
    """A working BAG (combo) order: conId 0, but its legs cover the underlying leg conIds."""
    def __init__(self, leg_cids, remaining):
        legs = [type("L", (), {"conId": c})() for c in leg_cids]
        self.contract = type("C", (), {"conId": 0, "comboLegs": legs, "symbol": "SPY"})()
        self.orderStatus = _Status("Submitted", 0)
        self.orderStatus.remaining = remaining
        self.order = type("O", (), {"totalQuantity": remaining})()

    def isDone(self):
        return False


def test_working_qty_sees_a_working_combo(cfg):
    """A BAG close has conId 0; without expanding its legs the idempotency guard is blind to it
    and would double-close (the reverse-spread-overnight bug)."""
    b = _broker(cfg)
    b.ib.openTrades = lambda: [_WorkingCombo([101, 102], 5)]
    assert b.working_qty(101) == 5      # leg is covered by the working combo
    assert b.working_qty(102) == 5
    assert b.working_qty(999) == 0      # unrelated leg


def test_close_legs_cancels_the_working_combo_first(cfg):
    """Per-leg escalation must cancel the in-flight combo close before covering, or both fill and
    double-close into the reverse spread held overnight."""
    b = _broker(cfg)
    short = type("C", (), {"conId": 101, "exchange": "", "localSymbol": "S"})()
    long_ = type("C", (), {"conId": 102, "exchange": "", "localSymbol": "L"})()
    ct = _Trade("Submitted", 0, total=5)                    # working combo close
    b.ib.positions = lambda: [_Position("OPT", cfg.symbol, -2, 101)]   # only short leg held
    b.close_legs_individually({"spread": {"short": short, "long": long_}, "close_trade": ct})
    assert ct.orderStatus.status == "Cancelled"            # combo cancelled BEFORE covering
    assert (b.ib.placed[0].action, b.ib.placed[0].totalQuantity) == ("BUY", 2)


# --- broker truth ---------------------------------------------------------------------------
def test_confirm_flat_is_broker_truth(cfg):
    b = _broker(cfg)
    b.ib.positions = lambda: [_Position("OPT", cfg.symbol, -2, 11)]
    assert b.confirm_flat([11], timeout=0.1) is False     # broker says we still hold it
    b.ib.positions = lambda: []
    assert b.confirm_flat([11], timeout=0.1) is True


def test_close_legs_individually_closes_only_what_is_held(cfg):
    """When the COMBO won't fill, close each leg from per-leg broker truth."""
    b = _broker(cfg)
    short = type("C", (), {"conId": 101, "exchange": "", "localSymbol": "SHORT"})()
    long_ = type("C", (), {"conId": 102, "exchange": "", "localSymbol": "LONG"})()
    # broker holds the short leg (-2); the long leg already expired/closed (flat)
    b.ib.positions = lambda: [_Position("OPT", cfg.symbol, -2, 101)]
    sent = b.close_legs_individually({"spread": {"short": short, "long": long_}})
    assert sent == 1
    assert (b.ib.placed[0].action, b.ib.placed[0].totalQuantity) == ("BUY", 2)
