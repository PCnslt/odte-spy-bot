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


# --- credit-spread decision helpers (pure, unit-tested) ---------------------------
def gap_exceeds(gap: float | None, threshold_pct: float) -> bool:
    """Opening-gap guard predicate. None (gap unknown) -> False: don't block on missing
    telemetry, the anomaly detector still protects intraday."""
    if gap is None:
        return False
    return abs(gap) >= threshold_pct


def assign_arm(ts_iso_minute: str, arms: list) -> object:
    """Deterministic per-trade experiment-arm assignment: stable md5 hash of the entry
    minute. Removes selection bias without any state; reproducible from the TradeLog."""
    import hashlib

    h = int(hashlib.md5(ts_iso_minute.encode()).hexdigest(), 16)
    return arms[h % len(arms)]
def spread_ev(credit: float, p_breach: float, pt_frac: float = 0.5,
              stop_mult: float = 2.0) -> float:
    """Premium-richness proxy CONSISTENT WITH THE EXIT STRUCTURE.

    Outcomes are approximately binary under our management: no-breach -> profit target
    (+pt_frac x credit); breach -> stop (-(stop_mult - 1) x credit). So
        EV ~= credit x [ pt_frac x (1 - p) - (stop_mult - 1) x p ]
    With pt=0.5, stop=2x this is positive iff P(breach) < 1/3. (A width-based EV is WRONG
    here: max loss requires expiry through the long strike, but our stop fires at a touch —
    loss-given-breach ~= credit, not width. The width version blocked 100% of entries.)"""
    p = min(max(p_breach, 0.0), 1.0)
    return credit * (pt_frac * (1 - p) - (stop_mult - 1) * p)



def defense_triggered(kind: str, spot: float, short_strike: float, buffer_pct: float) -> bool:
    """True when the underlying threatens the SHORT strike and the spread should be closed
    defensively, regardless of premium P&L. bull_put: danger is spot falling to the strike;
    bear_call: danger is spot rising to it. `buffer_pct` fires the exit slightly BEFORE the
    strike is touched (e.g. 0.001 = 0.1% early)."""
    if kind == "bull_put":
        return spot <= short_strike * (1 + buffer_pct)
    if kind == "bear_call":
        return spot >= short_strike * (1 - buffer_pct)
    return False


def liquidity_ok(short_bid: float, short_ask: float, long_bid: float, long_ask: float,
                 credit: float, max_frac: float) -> bool:
    """True when the cost of crossing both legs' half-spreads is a tolerable fraction of
    the credit. This is the gate on 'will transaction costs eat this trade'."""
    if credit <= 0:
        return False
    for v in (short_bid, short_ask, long_bid, long_ask):
        if v is None or not (v == v) or v < 0:   # None/NaN/negative -> can't assess
            return False
    half_spreads = ((short_ask - short_bid) + (long_ask - long_bid)) / 2.0
    if half_spreads < 0:
        return False
    return half_spreads <= max_frac * credit
