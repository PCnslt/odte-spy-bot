"""Performance monitoring. Rolling win rate, profit factor, Sharpe, max drawdown, expectancy."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..common import TradeResult


@dataclass
class PerformanceReport:
    total_trades: int
    win_rate: float
    profit_factor: float
    expectancy: float
    sharpe: float
    max_drawdown: float
    total_pnl: float
    avg_hold_minutes: float

    def as_dict(self) -> dict:
        return self.__dict__.copy()

    def pretty(self) -> str:
        return (
            f"trades={self.total_trades}  win={self.win_rate:.1%}  pf={self.profit_factor:.2f}  "
            f"exp=${self.expectancy:.2f}  sharpe={self.sharpe:.2f}  "
            f"maxDD=${self.max_drawdown:.2f}  pnl=${self.total_pnl:.2f}"
        )


class PerformanceMonitor:
    def __init__(self, retrain_window: int = 50, retrain_win_rate: float = 0.40):
        self.trades: list[TradeResult] = []
        self.retrain_window = retrain_window
        self.retrain_win_rate = retrain_win_rate

    def update(self, trade: TradeResult) -> None:
        self.trades.append(trade)

    def report(self) -> PerformanceReport:
        return summarize(self.trades)

    def rolling_win_rate(self, n: int | None = None) -> float:
        n = n or self.retrain_window
        recent = self.trades[-n:]
        if not recent:
            return 0.0
        return sum(t.won for t in recent) / len(recent)

    def should_retrain(self) -> bool:
        """True if the recent window has degraded below the acceptable win rate."""
        if len(self.trades) < self.retrain_window:
            return False
        return self.rolling_win_rate() < self.retrain_win_rate


def summarize(trades: list[TradeResult]) -> PerformanceReport:
    if not trades:
        return PerformanceReport(0, 0, 0, 0, 0, 0, 0, 0)
    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]

    gross_win = wins.sum()
    gross_loss = -losses.sum()
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
    win_rate = len(wins) / len(pnls)
    expectancy = pnls.mean()
    sharpe = (pnls.mean() / pnls.std() * np.sqrt(len(pnls))) if pnls.std() > 0 else 0.0

    equity_curve = np.cumsum(pnls)
    running_max = np.maximum.accumulate(equity_curve)
    max_dd = float((running_max - equity_curve).max()) if len(equity_curve) else 0.0

    avg_hold = float(np.mean([t.hold_minutes for t in trades]))
    return PerformanceReport(
        total_trades=len(trades), win_rate=win_rate, profit_factor=profit_factor,
        expectancy=expectancy, sharpe=sharpe, max_drawdown=max_dd,
        total_pnl=float(pnls.sum()), avg_hold_minutes=avg_hold,
    )
