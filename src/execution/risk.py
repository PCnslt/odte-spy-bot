"""Risk math on the REAL option premium. No Black-Scholes, no delta mapping.

  * Stop distance = clamp(sl_atr_mult * ATR(option's own recent bars),
                          sl_min_frac * entry, sl_max_frac * entry).
  * Take-profit distance = risk_reward_ratio * stop distance.
  * Size so (entry - stop) * 100 * contracts ~= risk_pct * equity, capped by max_contracts.

Everything here is driven by observed option prices (backtest: real Polygon bars; live: real
IBKR quotes/bars). Nothing is modeled.
"""
from __future__ import annotations

from dataclasses import dataclass


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
        t = r["targets"]
        self.rr = t["risk_reward_ratio"]
        self.sl_atr_mult = t["sl_atr_mult"]
        self.sl_min_frac = t["sl_min_frac"]
        self.sl_max_frac = t["sl_max_frac"]

    def stop_target(self, entry_premium: float, option_atr: float) -> StopTarget:
        """Compute stop/target from the real entry premium and the option's own ATR."""
        raw = self.sl_atr_mult * max(option_atr, 0.0)
        lo = self.sl_min_frac * entry_premium
        hi = self.sl_max_frac * entry_premium
        sl_distance = min(max(raw, lo), hi)
        sl_distance = max(sl_distance, 0.01)

        stop_loss = max(entry_premium - sl_distance, 0.01)
        take_profit = entry_premium + sl_distance * self.rr
        risk_per_contract = (entry_premium - stop_loss) * 100
        return StopTarget(round(stop_loss, 2), round(take_profit, 2), risk_per_contract)

    def size(self, equity: float, risk_per_contract: float) -> int:
        if risk_per_contract <= 0:
            return self.min_contracts
        target_risk = self.risk_pct * equity
        contracts = int(target_risk // risk_per_contract)
        return max(self.min_contracts, min(self.max_contracts, contracts))
