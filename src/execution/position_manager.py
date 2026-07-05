"""Position manager: converts a Signal into a sized, validated TradeIntent and enforces the
daily guardrails (max trades, daily-loss halt, concurrency, no-new-trades window)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from ..common import MarketSnapshot, OptionRight, Signal, TradeIntent
from ..utils.logger import get_logger
from .pricing import atm_strike, black_scholes
from .risk import RiskCalculator

log = get_logger("position_manager")


class PositionManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.calc = RiskCalculator(cfg)
        limits = cfg.risk["limits"]
        self.max_trades_per_day = limits["max_trades_per_day"]
        self.max_daily_loss_pct = limits["max_daily_loss_pct"]
        self.max_concurrent = limits["max_concurrent_positions"]
        self.strike_offset = cfg.execution.option.get("strike_offset", 0)

        self._day: Optional[date] = None
        self.trades_today = 0
        self.realized_pnl_today = 0.0
        self.halted = False

    # --- daily bookkeeping -----------------------------------------------------
    def _roll_day(self, now: datetime) -> None:
        d = now.date()
        if self._day is None:
            # First observation of the day: adopt it without wiping fresh counters.
            self._day = d
            return
        if self._day != d:
            self._day = d
            self.trades_today = 0
            self.realized_pnl_today = 0.0
            self.halted = False

    def record_result(self, pnl: float) -> None:
        self.realized_pnl_today += pnl

    def can_open(self, now: datetime, equity: float, open_count: int) -> tuple[bool, str]:
        self._roll_day(now)
        if self.halted:
            return False, "halted"
        if open_count >= self.max_concurrent:
            return False, "max_concurrent"
        if self.trades_today >= self.max_trades_per_day:
            return False, "max_trades_per_day"
        if self.realized_pnl_today <= -self.max_daily_loss_pct * equity:
            self.halted = True
            log.warning("Daily loss halt tripped: pnl=%.2f", self.realized_pnl_today)
            return False, "daily_loss_halt"
        return True, "ok"

    # --- intent construction ---------------------------------------------------
    def build_intent(self, signal: Signal, snapshot: MarketSnapshot, equity: float,
                     minutes_to_close: float) -> Optional[TradeIntent]:
        if signal == Signal.NO_TRADE:
            return None
        right = OptionRight.CALL if signal == Signal.BUY_CALL else OptionRight.PUT
        offset = self.strike_offset if right == OptionRight.CALL else -self.strike_offset
        strike = atm_strike(snapshot.spy_price, offset=offset)

        greeks = black_scholes(snapshot.spy_price, strike, minutes_to_close, snapshot.iv, right)
        entry = greeks.price
        if entry <= 0.02:
            log.info("Skipping: modeled premium too small (%.3f)", entry)
            return None

        # Use direction-correct greeks for the stop/target math.
        snap = snapshot
        snap.delta = greeks.delta
        snap.theta = greeks.theta
        st = self.calc.stop_target(entry, snap)
        contracts = self.calc.size(equity, st.risk_per_contract)

        intent = TradeIntent(
            timestamp=snapshot.timestamp,
            signal=signal,
            right=right,
            strike=strike,
            quantity=contracts,
            entry_price=round(entry, 2),
            stop_loss=round(st.stop_loss, 2),
            take_profit=round(st.take_profit, 2),
            underlying_at_entry=snapshot.spy_price,
            reason=signal.value,
        )
        # Carry the IV used for entry so SimBroker/backtest re-price consistently.
        intent._iv = snapshot.iv  # type: ignore[attr-defined]
        return intent

    def on_open(self) -> None:
        self.trades_today += 1
