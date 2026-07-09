"""IBKRBroker: native bracket orders on Interactive Brokers via ib_insync.

Works against the IBKR **paper** account out of the box (paper_port) and, only when explicitly
confirmed, a live account (live_port + execution.live_confirmed). ib_insync is imported lazily.

CAVEATS (read before trusting this with money):
  * This has NOT been battle-tested against a live IBKR account in this repo. Paper-test it
    yourself and reconcile fills before going live.
  * IBKR's native bracket uses an OCA group so SL/TP cancel each other. The time-stop is
    enforced here by cancelling the bracket and sending a closing market order.
  * 0DTE SPY options settle same day; ensure your account is approved for options trading and
    has the right permissions/market data.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ..common import ExitReason, Fill, OptionRight, Position, TradeIntent, TradeResult
from ..utils.logger import get_logger
from .broker_base import Broker

log = get_logger("ibkr_broker")


class IBKRBroker(Broker):
    def __init__(self, cfg, mode: str = "paper"):
        self.cfg = cfg
        self.mode = mode
        ib = cfg.execution.ibkr
        self.host = ib.host
        self.port = ib.live_port if mode == "live" else ib.paper_port
        self.client_id = ib.client_id
        self.exchange = ib.exchange
        self.currency = ib.currency
        self.time_stop = timedelta(minutes=cfg.risk["limits"]["time_stop_minutes"])
        self.ib = None
        self._positions: list[Position] = []
        self._brackets: dict[str, dict] = {}  # order_id -> {parent, tp, sl trades}

    def connect(self) -> bool:
        if self.mode == "live" and not self.cfg.execution.get("live_confirmed", False):
            raise RuntimeError(
                "Live mode requires execution.live_confirmed: true in config. Refusing to "
                "connect to a live account without explicit confirmation."
            )
        try:
            from ib_insync import IB
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("ib_insync not installed. pip install -r requirements-extras.txt") from exc
        from ib_insync import IB

        self.ib = IB()
        self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=15)
        self.ib.reqMarketDataType(3)  # delayed-frozen fallback if no live subscription
        log.info("IBKR broker connected (%s) on %s:%d", self.mode, self.host, self.port)
        return True

    def account_value(self) -> float:
        for row in self.ib.accountSummary():
            if row.tag == "NetLiquidation":
                return float(row.value)
        return float(self.cfg.risk["account"]["starting_equity"])

    def _todays_expiry(self) -> str:
        return datetime.now().strftime("%Y%m%d")

    def _option(self, strike: float, right: OptionRight):
        from ib_insync import Option
        opt = Option(self.cfg.symbol, self._todays_expiry(), strike, right.value,
                     self.exchange, currency=self.currency)
        self.ib.qualifyContracts(opt)
        return opt

    def place_bracket(self, intent: TradeIntent) -> Fill:
        opt = self._option(intent.strike, intent.right)
        bracket = self.ib.bracketOrder(
            "BUY", intent.quantity,
            limitPrice=intent.entry_price,
            takeProfitPrice=intent.take_profit,
            stopLossPrice=intent.stop_loss,
        )
        trades = [self.ib.placeOrder(opt, o) for o in bracket]
        parent = trades[0]
        oid = str(parent.order.orderId)
        self._brackets[oid] = {"contract": opt, "trades": trades}
        self._positions.append(Position(
            open_time=intent.timestamp, right=intent.right, strike=intent.strike,
            quantity=intent.quantity, entry_price=intent.entry_price,
            stop_loss=intent.stop_loss, take_profit=intent.take_profit,
            underlying_at_entry=intent.underlying_at_entry,
            iv_at_entry=0.0, order_id=oid,
        ))
        log.info("IBKR bracket sent: %s %s x%d", intent.right.value, intent.strike,
                 intent.quantity)
        return Fill(intent.timestamp, intent.right, intent.strike, intent.quantity,
                    intent.entry_price, "BUY", oid)

    def open_positions(self) -> list[Position]:
        return list(self._positions)

    def poll_exits(self, underlying_price: float, now: datetime) -> list[TradeResult]:
        """Reconcile native bracket fills; enforce our own time-stop on top."""
        self.ib.sleep(0)  # let ib_insync process events
        closed: list[TradeResult] = []
        still: list[Position] = []
        for pos in self._positions:
            info = self._brackets.get(pos.order_id, {})
            trades = info.get("trades", [])
            done = any(t.orderStatus.status == "Filled" for t in trades[1:])  # tp or sl filled
            timed_out = now - pos.open_time >= self.time_stop
            if done:
                exit_px, reason = self._filled_exit(trades)
                closed.append(self._result(pos, exit_px, reason, now, underlying_price))
            elif timed_out:
                self._cancel_and_close(info, pos)
                exit_px = pos.entry_price  # placeholder; reconcile from execution reports
                closed.append(self._result(pos, exit_px, ExitReason.TIME_STOP, now,
                                           underlying_price))
            else:
                still.append(pos)
        self._positions = still
        return closed

    def _filled_exit(self, trades) -> tuple[float, ExitReason]:
        for t in trades[1:]:
            if t.orderStatus.status == "Filled":
                px = t.orderStatus.avgFillPrice or t.order.lmtPrice or 0.0
                reason = ExitReason.TAKE_PROFIT if t.order.orderType == "LMT" else ExitReason.STOP_LOSS
                return float(px), reason
        return 0.0, ExitReason.STOP_LOSS

    def _cancel_and_close(self, info: dict, pos: Position) -> None:
        from ib_insync import MarketOrder
        for t in info.get("trades", []):
            if t.orderStatus.status not in ("Filled", "Cancelled"):
                self.ib.cancelOrder(t.order)
        self.ib.placeOrder(info["contract"], MarketOrder("SELL", pos.quantity))

    def flatten(self, now: datetime, reason: ExitReason = ExitReason.FLATTEN) -> list[TradeResult]:
        from ib_insync import MarketOrder
        closed = []
        for pos in self._positions:
            info = self._brackets.get(pos.order_id, {})
            self._cancel_and_close(info, pos)
            closed.append(self._result(pos, pos.entry_price, reason, now,
                                       pos.underlying_at_entry))
        self._positions = []
        return closed

    def _result(self, pos: Position, exit_px: float, reason: ExitReason,
                now: datetime, underlying: float) -> TradeResult:
        return TradeResult(
            open_time=pos.open_time, close_time=now, right=pos.right, strike=pos.strike,
            quantity=pos.quantity, entry_price=pos.entry_price, exit_price=exit_px,
            exit_reason=reason, underlying_at_entry=pos.underlying_at_entry,
            underlying_at_exit=underlying, commission=0.65 * pos.quantity * 2,
        )

    # --- credit spreads (combo/BAG orders) --------------------------------------
    def place_credit_spread(self, spread: dict, quantity: int, min_credit: float) -> Optional[dict]:
        """Open a defined-risk vertical as ONE atomic combo order.

        Legs: SELL the short strike, BUY the long strike. IBKR prices such a package
        negatively (you receive cash), so the entry limit is -(min_credit): 'fill only
        if I receive at least min_credit'. Returns a position dict or None if rejected.
        """
        from ib_insync import ComboLeg, Contract, LimitOrder

        combo = Contract(symbol=self.cfg.symbol, secType="BAG", exchange=self.exchange,
                         currency=self.currency)
        combo.comboLegs = [
            ComboLeg(conId=spread["short"].conId, ratio=1, action="SELL",
                     exchange=self.exchange),
            ComboLeg(conId=spread["long"].conId, ratio=1, action="BUY",
                     exchange=self.exchange),
        ]
        order = LimitOrder("BUY", quantity, round(-abs(min_credit), 2), tif="DAY")
        trade = self.ib.placeOrder(combo, order)
        # Audit M4/m3: bounded event-driven wait (not a blind sleep) — long enough to catch
        # an immediate rejection, short enough for the 30s loop. Fill tracking afterwards
        # is the existing per-poll state machine (unfilled entries auto-cancel at 3 min).
        deadline = 3.0
        while deadline > 0 and trade.orderStatus.status in ("PendingSubmit", "ApiPending"):
            self.ib.waitOnUpdate(timeout=0.25)
            deadline -= 0.25
        status = trade.orderStatus.status
        errors = [e.message for e in trade.log if e.errorCode not in (0, 399)]
        if errors:
            log.error("Spread order rejected: %s", errors)
            return None
        log.info("Spread combo sent (%s x%d, min credit %.2f): %s",
                 spread["kind"], quantity, min_credit, status)
        return {"combo": combo, "order": order, "trade": trade, "spread": spread,
                "quantity": quantity, "open_time": datetime.now(),
                "credit": abs(min_credit)}

    def close_credit_spread(self, pos: dict, limit_cost: Optional[float] = None):
        """Unwind the vertical. Market order by default (guaranteed exit). With
        `limit_cost`, place a LIMIT close at that cost instead — the combo is priced
        negatively, so paying at most `cost` means selling the package at -(cost). Returns
        the close Trade when one was placed (callers track fills / escalate), else None."""
        from ib_insync import LimitOrder, MarketOrder

        # Key off the ACTUAL filled quantity, never the status string. Incident 2026-07-08: an
        # unfilled entry stuck in "PendingSubmit" slipped past the old ("PreSubmitted","Submitted")
        # check, so instead of just cancelling we fired a MARKET SELL of a spread we never bought
        # -> a phantom SHORT position + P&L that didn't match the broker. Now: cancel any entry
        # still working, then only ever unwind the quantity that truly filled (0 -> do nothing).
        st = pos["trade"].orderStatus
        if st.status not in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
            self.ib.cancelOrder(pos["order"])
            self.ib.sleep(0)  # let the cancel/any-final-fill settle
        filled_qty = int(pos["trade"].orderStatus.filled or 0)
        if filled_qty <= 0:
            log.info("Spread entry unfilled (status=%s); cancelled, nothing to unwind.",
                     pos["trade"].orderStatus.status)
            return None
        pos["_unwound_qty"] = filled_qty   # caller records this size, not the requested qty
        if filled_qty != pos["quantity"]:
            log.warning("Partial entry fill: unwinding %d of %d requested.",
                        filled_qty, pos["quantity"])
        if limit_cost is not None:
            order = LimitOrder("SELL", filled_qty, round(-abs(limit_cost), 2), tif="DAY")
            trade = self.ib.placeOrder(pos["combo"], order)
            log.info("Spread close (LIMIT @ cost %.2f) sent x%d", limit_cost, filled_qty)
            return trade
        trade = self.ib.placeOrder(pos["combo"], MarketOrder("SELL", filled_qty))
        log.info("Spread close (market) sent x%d", filled_qty)
        return trade

    def escalate_close(self, pos: dict):
        """Cancel an unfilled limit close and exit at market instead. Returns the market
        Trade so the caller can keep tracking the ACTUAL fill (H3 instrumentation)."""
        from ib_insync import MarketOrder

        ct = pos.get("close_trade")
        qty = int(pos["quantity"])
        if ct is not None and ct.orderStatus.status not in ("Filled", "Cancelled"):
            # Market out ONLY what the limit close didn't already fill. Selling the full
            # requested qty when part had filled would over-sell → phantom SHORT (incident class).
            # Explicit None check, NOT `or`: remaining==0 is "fully filled", not "unknown".
            rem = ct.orderStatus.remaining
            qty = int(rem) if rem is not None else int(pos["quantity"])
            self.ib.cancelOrder(ct.order)
        if qty <= 0:
            log.info("Limit close already fully filled; nothing to escalate.")
            return ct
        trade = self.ib.placeOrder(pos["combo"], MarketOrder("SELL", qty))
        log.info("Limit close escalated to market x%d (of %d requested)", qty, pos["quantity"])
        return trade

    @staticmethod
    def close_fill(pos: dict) -> tuple[bool, Optional[float]]:
        """(filled?, actual close cost) for a pending limit close."""
        ct = pos.get("close_trade")
        if ct is None:
            return False, None
        st = ct.orderStatus
        if st.status == "Filled":
            return True, abs(st.avgFillPrice) if st.avgFillPrice else None
        return False, None

    def spread_fill_status(self, pos: dict) -> tuple[bool, float]:
        """(filled?, avg_fill_credit). Combo fill price is negative for a credit."""
        self.ib.sleep(0)
        st = pos["trade"].orderStatus
        filled = st.status == "Filled"
        if filled and st.avgFillPrice and not pos.get("_fill_logged"):
            # Audit M4: log the RAW fill price once — the negative-price BAG convention has
            # never seen a real fill; this is the evidence that verifies the sign logic.
            log.info("COMBO FILL raw avgFillPrice=%s (expected NEGATIVE for a credit open)",
                     st.avgFillPrice)
            if st.avgFillPrice > 0:
                log.error("SIGN ANOMALY: entry combo filled at POSITIVE price %s — "
                          "credit accounting may be inverted; review before trusting P&L.",
                          st.avgFillPrice)
            pos["_fill_logged"] = True
        if filled and not st.avgFillPrice:
            log.warning("Entry filled but avgFillPrice=%r; falling back to the entry limit "
                        "%.2f as credit_fill — verify against the broker.",
                        st.avgFillPrice, pos["credit"])
        credit = abs(st.avgFillPrice) if filled and st.avgFillPrice else pos["credit"]
        return filled, credit

    # --- orphan reconciliation (self-audit R6: crash recovery) ----------------------
    def orphan_positions(self) -> list:
        """Real option positions sitting in the account that THIS process has no record of
        (e.g. after a mid-session crash). Anything here is unmanaged risk."""
        out = []
        for p in self.ib.positions():
            c = p.contract
            if getattr(c, "secType", "") == "OPT" and c.symbol == self.cfg.symbol \
                    and p.position:
                out.append(p)
        return out

    def flatten_orphans(self) -> int:
        """Fail-closed: close every orphaned option leg at market. An unmanaged 0DTE
        position is strictly worse than a realized exit."""
        from ib_insync import MarketOrder

        orphans = self.orphan_positions()
        for p in orphans:
            c = p.contract
            c.exchange = c.exchange or self.exchange
            action = "SELL" if p.position > 0 else "BUY"
            self.ib.placeOrder(c, MarketOrder(action, abs(int(p.position))))
            log.warning("ORPHAN flattened: %s %s x%d", action, c.localSymbol,
                        abs(int(p.position)))
        return len(orphans)

    def disconnect(self) -> None:
        if self.ib is not None:
            self.ib.disconnect()
