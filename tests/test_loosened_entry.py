"""Loosened entry (2026-07-07): with use_ml_gate off, enter on the mechanical rule alone."""
from __future__ import annotations

from datetime import datetime

from src.common import MarketSnapshot, Regime, Signal
from src.signals.signal_generator import SignalGenerator


def _snap(price, vwap, rvol=2.0, prob=0.50, regime=Regime.CHOP):
    return MarketSnapshot(timestamp=datetime(2026, 7, 8, 14, 0), spy_price=price, vwap=vwap,
                          rvol=rvol, high_5min=price + 0.5, low_5min=price - 0.5,
                          ml_prob_up=prob, regime=regime)


def test_config_is_loosened(cfg):
    assert cfg.signal.get("use_ml_gate") is False      # gate off
    assert cfg.signal.get("require_breakout") is False  # breakout dropped


def test_trades_long_on_rule_despite_coinflip_ml(cfg):
    gen = SignalGenerator(cfg)
    # above VWAP, high RVOL, ml_prob a coin-flip 0.50 that would FAIL the old 0.55 gate
    d = gen.generate(_snap(price=501.0, vwap=499.0, prob=0.50))
    assert d.signal == Signal.BUY_CALL and "rule" in d.reason


def test_trades_short_below_vwap(cfg):
    gen = SignalGenerator(cfg)
    d = gen.generate(_snap(price=497.0, vwap=499.0, prob=0.50))
    assert d.signal == Signal.BUY_PUT


def test_rvol_gate_still_holds(cfg):
    gen = SignalGenerator(cfg)
    # low RVOL -> no rule trigger -> still no trade even with the ml gate off
    assert gen.generate(_snap(price=501.0, vwap=499.0, rvol=0.5)).signal == Signal.NO_TRADE


def test_volatile_regime_still_blocks(cfg):
    gen = SignalGenerator(cfg)
    # volatile safety floor stays active (prob near 0.5 -> low conviction -> no trade)
    d = gen.generate(_snap(price=501.0, vwap=499.0, prob=0.50, regime=Regime.VOLATILE))
    assert d.signal == Signal.NO_TRADE
