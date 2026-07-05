"""SimBroker: the default paper-trading / backtest broker.

Fills are modeled from the underlying via Black-Scholes, with pessimistic, explicit frictions:
  * entry: pay half the modeled bid-ask spread + a flat slippage tick.
  * exit:  give up the same.
  * commission: per-contract, both legs.
Stops/targets/time-stop are checked on each `poll_exits`. IV is held at its entry value between
polls (a simplification — real IV moves, usually against you into a stop). Documented, not hidden.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ..common import ExitReason, Fill, OptionRight, Position, TradeIntent, TradeResult
from ..utils.logger import get_logger
from .broker_base import Broker
from .pricing import black_scholes

log = get_logger("sim_broker")


class SimBroker(Broker):
    def __init__(self, cfg, starting_equity: Optional[float] = None):
        self.cfg = cfg
        self.equity = starting_equity if starting_equity is not None \
            else cfg.risk["account"]["starting_equity"]
        self.commission_per_contract = 0.65
        self.slippage = 0.03            # flat premium tick each side
        self.spread_frac = 0.02         # modeled half-spread as fraction of premium
        self.time_stop = timedelta(minutes=cfg.risk["limits"]["time_stop_minutes"])
        self._positions: list[Position] = []
        self._order_seq = 0

    def connect(self) -> bool:
        return True

    def account_value(self) -> float:
        return self.equity

    def _next_id(self) -> str:
        self._order_seq += 1
        return f"SIM-{self._order_seq}"

    def _friction(self, premium: float) -> float:
        return premium * self.spread_frac + self.slippage

    def place_bracket(self, intent: TradeIntent) -> Fill:
        fill_price = intent.entry_price + self._friction(intent.entry_price)  # buy at ask+slip
        oid = self._next_id()
        # iv_at_entry is recovered from the intent's entry premium via the snapshot's iv is not
        # available here; we store the entry premium and re-derive iv implicitly by holding the
        # modeled price consistent (we re-price with the same iv passed through intent metadata).
        pos = Position(
            open_time=intent.timestamp,
            right=intent.right,
            strike=intent.strike,
            quantity=intent.quantity,
            entry_price=fill_price,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            underlying_at_entry=intent.underlying_at_entry,
            iv_at_entry=getattr(intent, "_iv", 0.20),
            order_id=oid,
        )
        self._positions.append(pos)
        log.info("SIM open %s %s x%d @ %.2f (SL %.2f / TP %.2f)", intent.right.value,
                 intent.strike, intent.quantity, fill_price, intent.stop_loss, intent.take_profit)
        return Fill(intent.timestamp, intent.right, intent.strike, intent.quantity,
                    fill_price, "BUY", oid)

    def open_positions(self) -> list[Position]:
        return list(self._positions)

    def _price_option(self, pos: Position, underlying: float, now: datetime) -> float:
        minutes_left = self._minutes_to_close(now)
        greeks = black_scholes(underlying, pos.strike, minutes_left, pos.iv_at_entry, pos.right)
        return greeks.price

    def _minutes_to_close(self, now: datetime) -> float:
        ch, cm = map(int, self.cfg.session.market_close.split(":"))
        return float(max((ch * 60 + cm) - (now.hour * 60 + now.minute), 1))

    def poll_exits(self, underlying_price: float, now: datetime) -> list[TradeResult]:
        closed: list[TradeResult] = []
        still_open: list[Position] = []
        for pos in self._positions:
            mid = self._price_option(pos, underlying_price, now)
            exit_bid = max(mid - self._friction(mid), 0.0)   # sell at bid-slip
            reason = None
            if exit_bid <= pos.stop_loss:
                reason, exit_px = ExitReason.STOP_LOSS, pos.stop_loss
            elif exit_bid >= pos.take_profit:
                reason, exit_px = ExitReason.TAKE_PROFIT, pos.take_profit
            elif now - pos.open_time >= self.time_stop:
                reason, exit_px = ExitReason.TIME_STOP, exit_bid
            if reason is None:
                still_open.append(pos)
                continue
            closed.append(self._close(pos, exit_px, reason, now, underlying_price))
        self._positions = still_open
        return closed

    def flatten(self, now: datetime, reason: ExitReason = ExitReason.FLATTEN) -> list[TradeResult]:
        closed = []
        # Use last known price via re-pricing at strike-neutral: caller should pass a fresh poll
        # first; here we price at each position's underlying_at_entry as a fallback.
        for pos in self._positions:
            mid = self._price_option(pos, pos.underlying_at_entry, now)
            exit_px = max(mid - self._friction(mid), 0.0)
            closed.append(self._close(pos, exit_px, reason, now, pos.underlying_at_entry))
        self._positions = []
        return closed

    def _close(self, pos: Position, exit_px: float, reason: ExitReason,
               now: datetime, underlying: float) -> TradeResult:
        commission = self.commission_per_contract * pos.quantity * 2
        result = TradeResult(
            open_time=pos.open_time, close_time=now, right=pos.right, strike=pos.strike,
            quantity=pos.quantity, entry_price=pos.entry_price, exit_price=exit_px,
            exit_reason=reason, underlying_at_entry=pos.underlying_at_entry,
            underlying_at_exit=underlying, commission=commission,
        )
        self.equity += result.pnl
        log.info("SIM close %s @ %.2f (%s) pnl=%.2f equity=%.2f", pos.strike, exit_px,
                 reason.value, result.pnl, self.equity)
        return result
