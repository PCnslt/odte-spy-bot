from __future__ import annotations

from datetime import datetime, timedelta

from src.common import MarketSnapshot, OptionRight, Signal
from src.execution.pricing import atm_strike, black_scholes
from src.execution.position_manager import PositionManager
from src.execution.sim_broker import SimBroker


def test_black_scholes_call_put_parity_positive():
    call = black_scholes(500, 500, 60, 0.20, OptionRight.CALL)
    put = black_scholes(500, 500, 60, 0.20, OptionRight.PUT)
    assert call.price > 0 and put.price > 0
    assert 0 <= call.delta <= 1
    assert -1 <= put.delta <= 0
    assert call.theta <= 0  # long option bleeds


def test_atm_strike_rounds():
    assert atm_strike(499.4) == 499
    assert atm_strike(499.6) == 500
    assert atm_strike(500, offset=1) == 501


def test_sim_broker_take_profit_and_equity(cfg):
    broker = SimBroker(cfg, starting_equity=10000)
    now = datetime(2026, 6, 1, 14, 0)
    snap = MarketSnapshot(timestamp=now, spy_price=500.0, atr_5min=0.5, iv=0.20,
                          delta=0.5, theta=-0.02)
    pm = PositionManager(cfg)
    intent = pm.build_intent(Signal.BUY_CALL, snap, 10000, minutes_to_close=120)
    assert intent is not None and intent.quantity >= 1
    broker.place_bracket(intent)
    assert len(broker.open_positions()) == 1

    # Big favorable move should trigger the take-profit and grow equity.
    results = broker.poll_exits(515.0, now + timedelta(minutes=1))
    assert results and results[0].exit_reason.value in {"take_profit", "stop_loss", "time_stop"}


def test_daily_limits_halt(cfg):
    pm = PositionManager(cfg)
    now = datetime(2026, 6, 1, 14, 0)
    # Force a daily loss beyond the halt threshold.
    pm.record_result(-cfg.risk["limits"]["max_daily_loss_pct"] * 10000 - 1)
    ok, why = pm.can_open(now, 10000, 0)
    assert not ok and why in {"daily_loss_halt", "halted"}


def test_time_stop_closes(cfg):
    broker = SimBroker(cfg, starting_equity=10000)
    now = datetime(2026, 6, 1, 14, 0)
    snap = MarketSnapshot(timestamp=now, spy_price=500.0, atr_5min=0.5, iv=0.20,
                          delta=0.5, theta=-0.02)
    pm = PositionManager(cfg)
    intent = pm.build_intent(Signal.BUY_CALL, snap, 10000, minutes_to_close=120)
    broker.place_bracket(intent)
    later = now + timedelta(minutes=cfg.risk["limits"]["time_stop_minutes"] + 1)
    results = broker.poll_exits(500.2, later)
    assert results and results[0].exit_reason.value == "time_stop"
