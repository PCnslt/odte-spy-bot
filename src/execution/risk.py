"""Risk math: volatility-adjusted stop/target on the OPTION premium, and position sizing.

The rule set:
  * Translate an ATR-based move in the UNDERLYING into an option-premium move via delta.
  * Add a theta cushion to the stop (0DTE premium bleeds even if the underlying is flat).
  * Take-profit distance = risk_reward_ratio * stop distance.
  * Size so that (entry - stop) * 100 * contracts ~= risk_pct * equity, capped by max_contracts.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..common import MarketSnapshot, OptionRight, Signal


@dataclass
class StopTarget:
    stop_loss: float
    take_profit: float
    risk_per_contract: float   # dollars at risk per contract if stopped


class RiskCalculator:
    def __init__(self, cfg):
        r = cfg.risk
        self.risk_pct = r["per_trade"]["risk_pct"]
        self.max_contracts = r["per_trade"]["max_contracts"]
        self.min_contracts = r["per_trade"]["min_contracts"]
        self.rr = r["targets"]["risk_reward_ratio"]
        self.sl_atr_mult = r["targets"]["sl_atr_mult"]
        self.theta_buffer_min = r["targets"]["theta_buffer_minutes"]

    def stop_target(self, entry_premium: float, snapshot: MarketSnapshot) -> StopTarget:
        # Underlying move at the stop -> premium move via |delta|.
        underlying_move = self.sl_atr_mult * max(snapshot.atr_5min, 0.01)
        premium_move = abs(snapshot.delta) * underlying_move
        theta_cushion = abs(snapshot.theta) * self.theta_buffer_min
        sl_distance = max(premium_move + theta_cushion, 0.01)

        stop_loss = max(entry_premium - sl_distance, 0.01)
        take_profit = entry_premium + sl_distance * self.rr
        risk_per_contract = (entry_premium - stop_loss) * 100
        return StopTarget(stop_loss, take_profit, risk_per_contract)

    def size(self, equity: float, risk_per_contract: float) -> int:
        if risk_per_contract <= 0:
            return self.min_contracts
        target_risk = self.risk_pct * equity
        contracts = int(target_risk // risk_per_contract)
        return max(self.min_contracts, min(self.max_contracts, contracts))
