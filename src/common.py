"""Shared enums and dataclasses. Everything imports types from here so they stay consistent."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Signal(str, Enum):
    BUY_CALL = "BUY_CALL"
    BUY_PUT = "BUY_PUT"
    NO_TRADE = "NO_TRADE"


class Regime(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    CHOP = "chop"
    VOLATILE = "volatile"


class OptionRight(str, Enum):
    CALL = "C"
    PUT = "P"


class ExitReason(str, Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TIME_STOP = "time_stop"
    FLATTEN = "flatten"
    ANOMALY = "anomaly"


@dataclass
class MarketSnapshot:
    """A single point-in-time view of the market, consumed by the signal layer."""
    timestamp: datetime
    spy_price: float
    spy_volume: float = 0.0
    vwap: float = float("nan")
    atr_5min: float = float("nan")
    rvol: float = 1.0
    high_5min: float = float("nan")
    low_5min: float = float("nan")
    iv: float = 0.20              # annualized IV proxy for the ATM 0DTE option
    iv_percentile: float = 50.0
    vix: float = float("nan")
    delta: float = 0.5           # ATM call delta proxy
    gamma: float = 0.0
    theta: float = 0.0
    put_call_ratio: float = 1.0
    sentiment_score: float = 0.0
    regime: Regime = Regime.CHOP
    ml_prob_up: float = 0.5
    features: dict = field(default_factory=dict)


@dataclass
class TradeIntent:
    """A validated, sized order the risk layer hands to the broker."""
    timestamp: datetime
    signal: Signal
    right: OptionRight
    strike: float
    quantity: int
    entry_price: float           # option premium (per share)
    stop_loss: float             # option premium
    take_profit: float           # option premium
    underlying_at_entry: float
    reason: str = ""


@dataclass
class Fill:
    timestamp: datetime
    right: OptionRight
    strike: float
    quantity: int
    price: float
    side: str                    # "BUY" or "SELL"
    order_id: str = ""


@dataclass
class Position:
    open_time: datetime
    right: OptionRight
    strike: float
    quantity: int
    entry_price: float
    stop_loss: float
    take_profit: float
    underlying_at_entry: float
    iv_at_entry: float
    order_id: str = ""


@dataclass
class TradeResult:
    open_time: datetime
    close_time: datetime
    right: OptionRight
    strike: float
    quantity: int
    entry_price: float
    exit_price: float
    exit_reason: ExitReason
    underlying_at_entry: float
    underlying_at_exit: float
    commission: float = 0.0

    @property
    def pnl(self) -> float:
        """Net P&L in dollars. Options are x100 multiplier."""
        gross = (self.exit_price - self.entry_price) * self.quantity * 100
        return gross - self.commission

    @property
    def return_pct(self) -> float:
        cost = self.entry_price * self.quantity * 100
        return self.pnl / cost if cost else 0.0

    @property
    def hold_minutes(self) -> float:
        return (self.close_time - self.open_time).total_seconds() / 60.0

    @property
    def won(self) -> bool:
        return self.pnl > 0
