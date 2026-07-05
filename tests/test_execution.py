from __future__ import annotations

from datetime import datetime

from src.common import MarketSnapshot, OptionRight, Signal
from src.execution.position_manager import PositionManager
from src.execution.risk import RiskCalculator


def _snap(ts):
    return MarketSnapshot(timestamp=ts, spy_price=500.0, atr_5min=0.5, rvol=2.0,
                          vwap=499.0, high_5min=500.0, low_5min=498.0)


def test_stop_target_uses_option_premium_and_rr(cfg):
    calc = RiskCalculator(cfg)
    st = calc.stop_target(entry_premium=2.00, option_atr=0.40)
    assert st.stop_loss < 2.00 < st.take_profit
    # TP distance == rr * SL distance (both measured on the real premium).
    sl_dist = 2.00 - st.stop_loss
    tp_dist = st.take_profit - 2.00
    assert abs(tp_dist - cfg.risk["targets"]["risk_reward_ratio"] * sl_dist) < 1e-6
    assert st.risk_per_contract > 0


def test_stop_target_clamps_to_min_and_max_fraction(cfg):
    calc = RiskCalculator(cfg)
    # Tiny ATR -> stop floored at sl_min_frac of premium.
    st = calc.stop_target(entry_premium=1.00, option_atr=0.0)
    assert abs((1.00 - st.stop_loss) - cfg.risk["targets"]["sl_min_frac"]) < 1e-6
    # Huge ATR -> stop capped at sl_max_frac of premium.
    st2 = calc.stop_target(entry_premium=1.00, option_atr=99.0)
    assert abs((1.00 - st2.stop_loss) - cfg.risk["targets"]["sl_max_frac"]) < 1e-6


def test_sizing_respects_risk_and_caps(cfg):
    calc = RiskCalculator(cfg)
    # risk_per_contract tiny -> would size huge, but max_contracts caps it.
    assert calc.size(equity=100000, risk_per_contract=1.0) == cfg.risk["per_trade"]["max_contracts"]
    # risk_per_contract larger than budget -> min_contracts floor.
    assert calc.size(equity=1000, risk_per_contract=10000) == cfg.risk["per_trade"]["min_contracts"]


def test_build_intent_from_real_inputs(cfg):
    pm = PositionManager(cfg)
    now = datetime(2026, 6, 1, 14, 0)
    intent = pm.build_intent(Signal.BUY_CALL, _snap(now), equity=10000,
                             option_ticker="O:SPY260601C00500000", strike=500.0,
                             entry_premium=1.50, option_atr=0.30)
    assert intent is not None
    assert intent.right == OptionRight.CALL
    assert intent.quantity >= 1
    assert intent.stop_loss < intent.entry_price < intent.take_profit
    assert intent.option_ticker.startswith("O:SPY")


def test_build_intent_rejects_penny_premium(cfg):
    pm = PositionManager(cfg)
    now = datetime(2026, 6, 1, 14, 0)
    assert pm.build_intent(Signal.BUY_CALL, _snap(now), 10000,
                           "O:SPY260601C00500000", 500.0, 0.01, 0.1) is None


def test_daily_limits_halt(cfg):
    pm = PositionManager(cfg)
    now = datetime(2026, 6, 1, 14, 0)
    pm.record_result(-cfg.risk["limits"]["max_daily_loss_pct"] * 10000 - 1)
    ok, why = pm.can_open(now, 10000, 0)
    assert not ok and why in {"daily_loss_halt", "halted"}


def test_max_trades_per_day(cfg):
    pm = PositionManager(cfg)
    now = datetime(2026, 6, 1, 14, 0)
    pm.can_open(now, 10000, 0)  # establish the day
    for _ in range(cfg.risk["limits"]["max_trades_per_day"]):
        pm.on_open()
    ok, why = pm.can_open(now, 10000, 0)
    assert not ok and why == "max_trades_per_day"
