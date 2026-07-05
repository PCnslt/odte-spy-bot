"""Broker interface. Strategy code depends only on this ABC, never on a concrete broker."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..common import Fill, Position, TradeIntent, TradeResult


class Broker(ABC):
    @abstractmethod
    def connect(self) -> bool:
        ...

    @abstractmethod
    def account_value(self) -> float:
        """Current account equity in dollars."""

    @abstractmethod
    def place_bracket(self, intent: TradeIntent) -> Fill:
        """Open a position with attached stop-loss and take-profit. Returns the entry fill."""

    @abstractmethod
    def open_positions(self) -> list[Position]:
        ...

    @abstractmethod
    def poll_exits(self, underlying_price: float, now) -> list[TradeResult]:
        """Check SL/TP/time-stop against current market; close and return any completed trades."""

    @abstractmethod
    def flatten(self, now, reason) -> list[TradeResult]:
        """Close everything immediately (end-of-day, anomaly). Returns the closed trades."""

    def disconnect(self) -> None:  # optional
        pass
