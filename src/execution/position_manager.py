"""Position manager: converts a Signal + REAL option data into a sized, validated TradeIntent
and enforces the daily guardrails (max trades, daily-loss halt, concurrency).

No pricing model lives here. The caller resolves the actual 0DTE contract and passes the real
entry premium and the option's own ATR (from Polygon bars in backtest, IBKR bars/quotes live).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from ..common import MarketSnapshot, OptionRight, Signal, TradeIntent
from ..utils.logger import get_logger
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
        self.max_consecutive_losses = limits.get("max_consecutive_losses", 6)

        self._day: Optional[date] = None
        self.trades_today = 0
        self.realized_pnl_today = 0.0
        self.consecutive_losses = 0
        self.halted = False

    # --- daily bookkeeping -----------------------------------------------------
    def _roll_day(self, now: datetime) -> None:
        d = now.date()
        if self._day is None:
            self._day = d
            return
        if self._day != d:
            self._day = d
            self.trades_today = 0
            self.realized_pnl_today = 0.0
            self.halted = False

    def record_result(self, pnl: float) -> None:
        self.realized_pnl_today += pnl
        # Consecutive-loss brake: counts across days, resets on any win.
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.max_consecutive_losses:
                log.warning("Consecutive-loss brake: %d straight losses; pausing entries.",
                            self.consecutive_losses)
        else:
            self.consecutive_losses = 0

    def can_open(self, now: datetime, equity: float, open_count: int) -> tuple[bool, str]:
        self._roll_day(now)
        if self.halted:
            return False, "halted"
        if self.consecutive_losses >= self.max_consecutive_losses:
            return False, "consecutive_loss_brake"
        if open_count >= self.max_concurrent:
            return False, "max_concurrent"
        if self.trades_today >= self.max_trades_per_day:
            return False, "max_trades_per_day"
        if self.realized_pnl_today <= -self.max_daily_loss_pct * equity:
            self.halted = True
            log.warning("Daily loss halt tripped: pnl=%.2f", self.realized_pnl_today)
            return False, "daily_loss_halt"
        return True, "ok"

    # --- intent construction (real option inputs) ------------------------------
    def build_intent(self, signal: Signal, snapshot: MarketSnapshot, equity: float,
                     option_ticker: str, strike: float, entry_premium: float,
                     option_atr: float) -> Optional[TradeIntent]:
        if signal == Signal.NO_TRADE:
            return None
        if entry_premium <= 0.02:
            log.info("Skipping: real premium too small (%.3f) for %s", entry_premium,
                     option_ticker)
            return None
        right = OptionRight.CALL if signal == Signal.BUY_CALL else OptionRight.PUT

        st = self.calc.stop_target(entry_premium, option_atr)
        contracts = self.calc.size(equity, st.risk_per_contract)

        return TradeIntent(
            timestamp=snapshot.timestamp,
            signal=signal,
            right=right,
            strike=strike,
            quantity=contracts,
            entry_price=round(entry_premium, 2),
            stop_loss=st.stop_loss,
            take_profit=st.take_profit,
            underlying_at_entry=snapshot.spy_price,
            option_ticker=option_ticker,
            reason=signal.value,
        )

    def on_open(self) -> None:
        self.trades_today += 1
