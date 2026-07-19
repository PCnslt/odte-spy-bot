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

import time
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
        # 1=live (needs OPRA subscription) … 3=delayed (default). See config execution.ibkr.
        self.ib.reqMarketDataType(int(self.cfg.execution.ibkr.get("market_data_type", 3)))
        self._assert_account_matches_mode()
        log.info("IBKR broker connected (%s) on %s:%d", self.mode, self.host, self.port)
        return True

    def _assert_account_matches_mode(self) -> None:
        """The port is only a CONVENTION — whatever account the Gateway is logged into is the
        account we trade. IBKR paper accounts start with 'D' (e.g. DU…/DUR…); live accounts do
        not. Without this check, a Gateway logged into the LIVE account on the paper port would
        happily send real-money 0DTE orders under `--mode paper`. Fail closed."""
        accounts = [a for a in (self.ib.managedAccounts() or []) if a]
        if not accounts:
            raise RuntimeError("IBKR returned no managed accounts — refusing to trade.")
        # Paper accounts are 'DU' (individual) or 'DF' (advisor/family). Bare 'D' would accept a
        # live account that happens to start with D. False-reject is safe (bot just won't trade);
        # false-accept sends real money — so match the specific paper prefixes.
        is_paper = all(a.upper().startswith(("DU", "DF")) for a in accounts)
        if self.mode == "paper" and not is_paper:
            self.ib.disconnect()
            raise RuntimeError(
                f"REFUSING TO TRADE: --mode paper but the Gateway on port {self.port} is logged "
                f"into non-paper account(s) {accounts}. Paper accounts start with 'DU'/'DF'.")
        if self.mode == "live" and is_paper:
            raise RuntimeError(
                f"--mode live but account(s) {accounts} look like PAPER accounts. Refusing.")
        log.info("Account check OK: mode=%s accounts=%s", self.mode, accounts)

    def account_value(self) -> float:
        for row in self.ib.accountSummary():
            if row.tag == "NetLiquidation":
                return float(row.value)
        # Never silently substitute a config number for the real balance — a missing NetLiq tag
        # means the connection is broken/mis-authed, and the fallback made healthcheck pass.
        raise RuntimeError("NetLiquidation unavailable from IBKR — connection not healthy.")

    # --- broker-truth primitives ----------------------------------------------------
    # Every incident this bot has had came from the same root: local state was trusted over
    # the broker. `ib.sleep(0)` is ONE event-loop tick with no network round-trip, so it can
    # never confirm a cancel; and `orderStatus.status == "Filled"` misses Cancelled/ApiCancelled
    # while PendingCancel/Inactive are neither active nor done. These helpers make "did it
    # actually happen at IBKR?" answerable.
    DONE_STATES = ("Filled", "Cancelled", "ApiCancelled")

    def _await_terminal(self, trade, timeout: float = 5.0) -> Optional[str]:
        """Pump the event loop until `trade` reaches a TRUE terminal state (or timeout).
        This is what makes a cancel observable — without it, a fill in flight during the cancel
        is invisible and we over-sell (phantom short) or drop a live position."""
        if trade is None:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if trade.isDone():
                    break
            except Exception:
                if trade.orderStatus.status in self.DONE_STATES:
                    break
            self.ib.waitOnUpdate(timeout=0.25)
        return trade.orderStatus.status

    @staticmethod
    def _filled_qty(trade) -> int:
        """Filled quantity from the ACTUAL executions, falling back to orderStatus."""
        try:
            return int(trade.filled())
        except Exception:
            return int(trade.orderStatus.filled or 0)

    @staticmethod
    def _remaining_qty(trade) -> int:
        """Un-executed remainder. Sizing a replacement from order.totalQuantity instead of this
        is the classic over-sell bug."""
        try:
            return int(trade.remaining())
        except Exception:
            rem = trade.orderStatus.remaining
            return int(rem) if rem is not None else 0

    def refresh_positions(self, timeout: float = 2.0) -> None:
        """Force a real reqPositions round-trip so ib.positions() reflects TWS, not just the
        local event cache. ib.sleep(0) is one loop tick with NO round-trip, so a fill TWS hasn't
        pushed yet is invisible for a poll (the R3 one-poll cache-lag). Fail-soft: on any error
        keep the cache — never crash the trading loop over a refresh."""
        try:
            self.ib.reqPositions()
            self.ib.waitOnUpdate(timeout=timeout)   # pump until positionEnd is processed
        except Exception as exc:
            log.info("refresh_positions: reqPositions failed (%s); using cache.", exc)

    def positions_for(self, con_ids, refresh: bool = False) -> dict:
        """Broker truth: net position per conId (0 when flat). refresh=True forces a reqPositions
        round-trip first, closing the one-poll cache-lag window (R3)."""
        if refresh:
            self.refresh_positions()
        self.ib.sleep(0)
        out = {int(c): 0 for c in con_ids if c}
        for p in self.ib.positions():
            cid = int(getattr(p.contract, "conId", 0) or 0)
            if cid in out:
                out[cid] = int(p.position)
        return out

    def confirm_flat(self, con_ids, timeout: float = 5.0) -> bool:
        """Poll ib.positions() until every conId is flat. This — not order status — is the only
        acceptable proof that a spread is closed before we drop it from the book."""
        con_ids = [int(c) for c in con_ids if c]
        if not con_ids:
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            if all(v == 0 for v in self.positions_for(con_ids).values()):
                return True
            self.ib.waitOnUpdate(timeout=0.25)
        return all(v == 0 for v in self.positions_for(con_ids).values())

    @staticmethod
    def spread_con_ids(pos: dict) -> list:
        sp = pos.get("spread") or {}
        return [getattr(sp.get("short"), "conId", None), getattr(sp.get("long"), "conId", None)]

    def close_legs_individually(self, pos: dict) -> int:
        """Last resort when the COMBO won't fill (deep-ITM legs make the BAG illiquid even
        though each leg trades). Combos fill leg-by-leg and can end up single-legged, so read
        per-leg broker truth and close exactly what we actually hold. Returns orders sent."""
        from ib_insync import MarketOrder

        # Cancel any still-working COMBO close FIRST and wait for it to settle. A BAG order has
        # conId 0, so working_qty (which matches per-leg conIds) can't see it — if we skip this,
        # the working combo SELL and the per-leg covers can BOTH fill at the close and double-
        # close us into the REVERSE spread, held overnight. This is the incident class.
        ct = pos.get("close_trade")
        if ct is not None and ct.orderStatus.status not in self.DONE_STATES:
            self.ib.cancelOrder(ct.order)
            self._await_terminal(ct, timeout=5.0)

        sp = pos.get("spread") or {}
        legs = [sp.get("short"), sp.get("long")]
        held = self.positions_for([getattr(l, "conId", None) for l in legs])
        sent = 0
        for leg in legs:
            cid = int(getattr(leg, "conId", 0) or 0)
            qty = held.get(cid, 0)
            if not qty:
                continue
            working = self.working_qty(cid)          # never stack on a live order
            if working >= abs(qty):
                continue
            leg.exchange = leg.exchange or self.exchange
            action = "SELL" if qty > 0 else "BUY"
            self.ib.placeOrder(leg, MarketOrder(action, abs(qty) - working))
            log.warning("LEG-LEVEL close: %s %s x%d", action,
                        getattr(leg, "localSymbol", "") or cid, abs(qty) - working)
            sent += 1
        return sent

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
        # Rejection is decided by ORDER STATUS, never by "there is a log line". Many IBKR
        # messages (2109 pre/post-RTH, 2137, order-held notices) are benign warnings that arrive
        # while the order stays WORKING. Treating any of them as a rejection returned None while
        # the order remained live at IBKR -> it could fill into an untracked, unmanaged 0DTE
        # spread. Now: only a terminal-reject status kills it, and we cancel before giving up.
        msgs = [f"{e.errorCode}:{e.message}" for e in trade.log if e.errorCode not in (0, 399)]
        if status in ("Cancelled", "ApiCancelled", "Inactive"):
            log.error("Spread order rejected (status=%s): %s", status, msgs)
            self.ib.cancelOrder(order)          # belt & braces: ensure nothing is left working
            self._await_terminal(trade, timeout=2.0)
            return None
        if msgs:
            log.warning("Spread order warnings (order still %s, TRACKING it): %s", status, msgs)
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
        entry = pos["trade"]
        if entry.orderStatus.status not in self.DONE_STATES:
            self.ib.cancelOrder(pos["order"])
            # Wait for a REAL terminal state. `ib.sleep(0)` is one event-loop tick with no
            # network round-trip, so a fill racing the cancel was invisible — we'd read
            # filled=0 and drop a live position (or over-sell it).
            self._await_terminal(entry, timeout=5.0)
        filled_qty = self._filled_qty(entry)
        if filled_qty <= 0:
            log.info("Spread entry unfilled (status=%s); cancelled, nothing to unwind.",
                     entry.orderStatus.status)
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
        if ct is not None:
            if ct.orderStatus.status not in self.DONE_STATES:
                self.ib.cancelOrder(ct.order)
                # The cancel MUST settle before we read the remainder. Reading `remaining`
                # before the cancel is confirmed lets a fill land in the gap → we then market
                # out the full size on top of it → phantom SHORT.
                self._await_terminal(ct, timeout=5.0)
            # Size the replacement from the true un-executed remainder, never totalQuantity.
            qty = self._remaining_qty(ct)
        if qty <= 0:
            log.info("Close already fully filled; nothing to escalate.")
            return ct
        trade = self.ib.placeOrder(pos["combo"], MarketOrder("SELL", qty))
        log.info("Close escalated to market x%d (of %d requested)", qty, pos["quantity"])
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
    def working_qty(self, con_id) -> int:
        """Quantity ALREADY working at the broker for this contract.

        Idempotency guard. The per-poll reconciliation sweeps every 30s; pre-market a stock
        MarketOrder does not fill but can sit held, so a naive sweep would stack a fresh order
        each poll and then fill ALL of them at the open — turning a 200-share cover into a
        multi-thousand-share position. Never place what is already working."""
        con_id = int(con_id)
        total = 0
        try:
            trades = self.ib.openTrades()
        except Exception:
            return 0
        for t in trades:
            try:
                if t.isDone():
                    continue
                c = t.contract
                # A working COMBO (BAG, conId 0) covers this leg if the leg is one of its legs.
                # Without this, a still-working combo close is invisible and we double-cover.
                leg_cids = {int(getattr(cl, "conId", 0) or 0)
                            for cl in (getattr(c, "comboLegs", None) or [])}
                if int(getattr(c, "conId", 0) or 0) != con_id and con_id not in leg_cids:
                    continue
            except Exception:
                continue
            rem = t.orderStatus.remaining
            total += int(rem if rem is not None else (t.order.totalQuantity or 0))
        return total

    def orphan_positions(self, refresh: bool = True) -> list:
        """Real SPY positions the process isn't managing — orphaned option legs (after a crash
        or a close that never confirmed) AND shares from a 0DTE assignment. All unmanaged risk.
        Incident 2026-07-09: a breached bear-call whose market close didn't confirm was left
        open; the short call expired ITM and assigned into short stock — which the OPT-only
        sweep would never have cleared. Now STK is included.

        refresh=True (default) forces a reqPositions round-trip first so a leg that filled since
        the last processed event (e.g. a cancel/fill race) can't hide in the stale cache for a
        poll (R3, one-poll cache-lag). Callers gating NEW entries on this MUST keep the default."""
        if refresh:
            self.refresh_positions()
        out = []
        for p in self.ib.positions():
            c = p.contract
            if c.symbol == self.cfg.symbol and p.position \
                    and getattr(c, "secType", "") in ("OPT", "STK"):
                out.append(p)
        return out

    def flatten_orphans(self) -> int:
        """Fail-closed: market-close every orphaned SPY leg — options AND assigned shares. An
        unmanaged 0DTE position (or the naked stock a breached-and-assigned spread leaves) is
        strictly worse than a realized exit. Expired option legs can't be traded and simply
        fall away at settlement; the stock they assign into is what this must catch."""
        from ib_insync import MarketOrder

        # Cancel any still-working SPY order first (esp. a combo close, invisible to the per-leg
        # idempotency guard) so this account-wide sweep can't race it into a double execution.
        for t in list(self.ib.openTrades()):
            try:
                if getattr(t.contract, "symbol", "") == self.cfg.symbol and not t.isDone():
                    self.ib.cancelOrder(t.order)
                    self._await_terminal(t, timeout=3.0)
            except Exception:
                pass

        orphans = self.orphan_positions()
        sent = 0
        for p in orphans:
            c = p.contract
            need = abs(int(p.position))
            # IDEMPOTENT: never stack a second cover on top of one already working. The per-poll
            # sweep runs every 30s; pre-market a stock MarketOrder is held rather than filled, so
            # re-sending would queue N covers that ALL fill at the open.
            working = self.working_qty(getattr(c, "conId", 0))
            if working >= need:
                log.info("ORPHAN %s: %d already working at broker; not re-sending.",
                         getattr(c, "localSymbol", "") or c.symbol, working)
                continue
            # Assigned shares route to SMART; option legs keep their options exchange.
            c.exchange = "SMART" if getattr(c, "secType", "") == "STK" \
                else (c.exchange or self.exchange)
            action = "SELL" if p.position > 0 else "BUY"
            qty = need - working
            self.ib.placeOrder(c, MarketOrder(action, qty))
            log.warning("ORPHAN flattened: %s %s %s x%d", action,
                        getattr(c, "secType", ""), getattr(c, "localSymbol", "") or c.symbol, qty)
            sent += 1
        return sent

    def disconnect(self) -> None:
        if self.ib is not None:
            self.ib.disconnect()
