from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

from src.common import ExitReason, OptionRight, TradeResult
from src.learning.anomaly_detector import AnomalyAction, AnomalyDetector
from src.learning.evaluator import summarize
from src.learning.self_corrector import Adjustable, SelfCorrector
from src.utils.memory import TradingMemory


def _trade(pnl: float) -> TradeResult:
    t0 = datetime(2026, 6, 1, 14, 0)
    entry, exit_ = 1.0, 1.0 + pnl / 100.0
    return TradeResult(t0, t0 + timedelta(minutes=5), OptionRight.CALL, 500, 1,
                       entry, exit_, ExitReason.TIME_STOP, 500, 500, commission=0.0)


def test_summarize_basic():
    rep = summarize([_trade(50), _trade(-20), _trade(30)])
    assert rep.total_trades == 3
    assert 0 < rep.win_rate < 1
    assert rep.total_pnl != 0


def test_anomaly_price_shock_halts(cfg):
    det = AnomalyDetector(cfg)
    for _ in range(50):
        det.observe(0.0001, 0.20)
    res = det.check(0.05, 0.20)  # 5% one-minute move
    assert "PRICE_SHOCK" in res.kinds
    assert res.action == AnomalyAction.HALT


def test_self_corrector_derisks_on_losses():
    params = Adjustable(risk_pct=0.02, ml_threshold_long=0.62, ml_threshold_short=0.38,
                        sl_atr_mult=0.75)
    sc = SelfCorrector(params, historical_vol=0.20)
    from src.learning.evaluator import PerformanceReport
    bad = PerformanceReport(total_trades=30, win_rate=0.30, profit_factor=0.6, expectancy=-5,
                            sharpe=-0.5, max_drawdown=100, total_pnl=-150, avg_hold_minutes=6)
    out = sc.adjust(bad, current_vol=0.20)
    assert out.risk_pct < 0.02              # de-risked
    assert out.ml_threshold_long > 0.62     # more conviction required
    assert out.risk_pct >= out.RISK_MIN     # clamped


def test_memory_time_gate_and_whipsaw():
    with tempfile.TemporaryDirectory() as d:
        mem = TradingMemory(db_path=os.path.join(d, "m.db"), time_gate_minutes=3,
                            max_bias_changes_per_hour=2)
        now = datetime(2026, 6, 1, 14, 0)
        ok, _ = mem.check_consistency("SPY", "bullish", now=now)
        assert ok
        mem.store_decision("SPY", "bullish", now=now)
        # Within 3 minutes -> blocked by time gate.
        blocked, reason = mem.check_consistency("SPY", "bearish", now=now + timedelta(minutes=1))
        assert not blocked and reason == "time_gate"
        mem.close()
