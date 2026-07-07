"""Signal generator: fuse rules + ML probability + regime + sentiment + memory into one call.

Decision order (fail-closed):
  1. Hard gates: bearish-sentiment veto, volatile-regime conviction floor.
  2. Rule trigger: price vs VWAP band + breakout of the 5-minute range + RVOL confirmation.
  3. ML agreement: prob(up) must clear the long/short threshold in the same direction.
  4. Memory consistency: time gate + whipsaw guard (checked by the caller via TradingMemory).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..common import MarketSnapshot, Regime, Signal


@dataclass
class SignalDecision:
    signal: Signal
    reason: str
    ml_prob: float


class SignalGenerator:
    def __init__(self, cfg):
        s = cfg.signal
        self.ml_long = s.get("ml_threshold_long", 0.62)
        self.ml_short = s.get("ml_threshold_short", 0.38)
        self.min_rvol = s.get("min_rvol", 1.3)
        self.vwap_band = s.get("vwap_band_pct", 0.001)
        self.require_breakout = s.get("require_breakout", True)
        # The directional model carries NO edge (random-entry benchmark == full stack), and its
        # output is compressed near 0.5 so the ml_long/ml_short gate is often unreachable -> ~0
        # trades. With use_ml_gate False we enter on the mechanical rule alone (premium selling
        # doesn't need a direction forecast; VWAP side just picks bull-put vs bear-call).
        self.use_ml_gate = s.get("use_ml_gate", True)
        self.use_regime = s.get("use_regime_filter", True)
        self.volatile_min_prob = s.get("volatile_regime_min_prob", 0.68)
        self.bearish_veto = cfg.sentiment.get("bearish_veto", -0.7)

    def _rule_direction(self, s: MarketSnapshot) -> int:
        """+1 long setup, -1 short setup, 0 none (based on price/VWAP/breakout/RVOL)."""
        if s.rvol < self.min_rvol:
            return 0
        above_vwap = s.spy_price > s.vwap * (1 + self.vwap_band)
        below_vwap = s.spy_price < s.vwap * (1 - self.vwap_band)
        # Breakout confirmation is optional (research toggle). Without it, the ML probability
        # carries the timing and the VWAP side carries direction.
        breaks_high = s.spy_price >= s.high_5min if self.require_breakout else True
        breaks_low = s.spy_price <= s.low_5min if self.require_breakout else True
        if above_vwap and breaks_high:
            return 1
        if below_vwap and breaks_low:
            return -1
        return 0

    def generate(self, s: MarketSnapshot) -> SignalDecision:
        prob = s.ml_prob_up

        # 1. Sentiment veto (only blocks longs; a hard bearish read shouldn't force a put).
        if s.sentiment_score <= self.bearish_veto:
            long_blocked = True
        else:
            long_blocked = False

        # 2. Regime conviction floor.
        if self.use_regime and s.regime == Regime.VOLATILE:
            if abs(prob - 0.5) < (self.volatile_min_prob - 0.5):
                return SignalDecision(Signal.NO_TRADE, "volatile_low_conviction", prob)

        rule = self._rule_direction(s)
        if rule == 0:
            return SignalDecision(Signal.NO_TRADE, "no_rule_trigger", prob)

        # 3a. ML-free path: enter on the rule direction alone (see __init__ note).
        if not self.use_ml_gate:
            if rule > 0:
                if long_blocked:
                    return SignalDecision(Signal.NO_TRADE, "sentiment_veto_long", prob)
                return SignalDecision(Signal.BUY_CALL, "rule_long", prob)
            return SignalDecision(Signal.BUY_PUT, "rule_short", prob)

        # 3b. ML must agree with the rule direction.
        if rule > 0 and prob >= self.ml_long and not long_blocked:
            return SignalDecision(Signal.BUY_CALL, "rule+ml_long", prob)
        if rule < 0 and prob <= self.ml_short:
            return SignalDecision(Signal.BUY_PUT, "rule+ml_short", prob)

        if rule > 0 and long_blocked:
            return SignalDecision(Signal.NO_TRADE, "sentiment_veto_long", prob)
        return SignalDecision(Signal.NO_TRADE, "ml_disagree", prob)
