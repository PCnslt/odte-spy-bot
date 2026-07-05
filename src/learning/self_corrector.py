"""Self-corrector: bounded, auditable parameter nudges when performance drifts.

Deliberately conservative. It can only move a few knobs, each clamped to a safe range, and it
logs every change. It NEVER increases risk after a losing streak, and never exceeds hard caps.
This is a governor, not an optimizer — real re-optimization happens in the nightly retrain.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..utils.logger import get_logger
from .evaluator import PerformanceReport

log = get_logger("self_corrector")


@dataclass
class Adjustable:
    """The live-tunable subset of parameters, with hard bounds."""
    risk_pct: float
    ml_threshold_long: float
    ml_threshold_short: float
    sl_atr_mult: float

    # bounds
    RISK_MIN: float = field(default=0.005, repr=False)
    RISK_MAX: float = field(default=0.03, repr=False)
    THRESH_MIN: float = field(default=0.55, repr=False)
    THRESH_MAX: float = field(default=0.75, repr=False)
    SL_MIN: float = field(default=0.5, repr=False)
    SL_MAX: float = field(default=1.5, repr=False)

    def clamp(self) -> None:
        self.risk_pct = min(max(self.risk_pct, self.RISK_MIN), self.RISK_MAX)
        self.ml_threshold_long = min(max(self.ml_threshold_long, self.THRESH_MIN), self.THRESH_MAX)
        self.ml_threshold_short = 1.0 - self.ml_threshold_long
        self.sl_atr_mult = min(max(self.sl_atr_mult, self.SL_MIN), self.SL_MAX)


class SelfCorrector:
    def __init__(self, params: Adjustable, historical_vol: float = 0.20):
        self.params = params
        self.historical_vol = historical_vol
        self.history: list[dict] = []

    def adjust(self, report: PerformanceReport, current_vol: float) -> Adjustable:
        before = vars(self.params).copy()
        wr = report.win_rate

        if report.total_trades >= 20:
            if wr < 0.40:
                # Losing: de-risk and demand more conviction.
                self.params.risk_pct *= 0.8
                self.params.ml_threshold_long += 0.03
            elif wr > 0.60 and report.profit_factor > 1.3:
                # Winning cleanly: allow a small, capped risk increase.
                self.params.risk_pct *= 1.1

        # Volatility-aware stop widening.
        if current_vol > self.historical_vol * 1.3:
            self.params.sl_atr_mult *= 1.2
        elif current_vol < self.historical_vol * 0.7:
            self.params.sl_atr_mult *= 0.9

        self.params.clamp()
        after = vars(self.params).copy()
        if before != after:
            change = {"win_rate": wr, "before": before, "after": after}
            self.history.append(change)
            log.info("Self-correct: wr=%.2f risk=%.4f thr=%.2f sl=%.2f", wr,
                     self.params.risk_pct, self.params.ml_threshold_long, self.params.sl_atr_mult)
        return self.params
