from __future__ import annotations

import src.backtest as backtest
from src.data.data_pipeline import build_snapshot, build_training_set


def test_build_training_set_shapes(cfg, synthetic_bars):
    X, y = build_training_set(cfg, synthetic_bars)
    assert len(X) == len(y) > 0
    assert X.isna().sum().sum() == 0


def test_build_snapshot_offline(cfg, synthetic_bars):
    snap = build_snapshot(cfg, synthetic_bars, model=None, minutes_to_close=120)
    assert snap.spy_price > 0
    assert 0 <= snap.ml_prob_up <= 1
    assert snap.regime.value in {"trend_up", "trend_down", "chop", "volatile"}


def test_backtest_runs_end_to_end(cfg, synthetic_bars, monkeypatch):
    # Feed the backtester deterministic bars, no trained model -> rules-only path.
    monkeypatch.setattr(backtest, "load_bars", lambda *a, **k: synthetic_bars)
    result = backtest.run_backtest(cfg, days=1, verbose=False)
    assert "report" in result
    assert result["report"]["total_trades"] >= 0
    assert result["final_equity"] > 0
