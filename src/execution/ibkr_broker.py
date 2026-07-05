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
            iv_at_entry=getattr(intent, "_iv", 0.20), order_id=oid,
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

    def disconnect(self) -> None:
        if self.ib is not None:
            self.ib.disconnect()
