"""End-to-end wiring tests. These use synthetic bars + a fake Polygon client ON PURPOSE —
unit tests must be deterministic and offline. The real backtest/live paths only ever touch
real Polygon/IBKR data; nothing here is used at runtime."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import backtest
from src.data.data_pipeline import build_snapshot, build_training_set


class FakePolygon:
    """Serves option 'bars' derived from the SPY fixture so the fill-walk can be exercised."""

    def __init__(self, spy_bars: pd.DataFrame):
        self.spy = spy_bars

    def nearest_contract(self, expiry, right, spot, strike_offset=0):
        strike = float(round(spot)) + strike_offset
        return {"ticker": f"O:SPYFAKE{right}{int(strike*1000):08d}", "strike": strike,
                "type": right}

    def option_bars(self, ticker, day):
        # Intrinsic-ish premium path from the underlying, with a small high/low band.
        c = self.spy["close"]
        prem = np.maximum(c - c.iloc[0], 0) * 0.5 + 1.0
        return pd.DataFrame({
            "open": prem, "high": prem * 1.03, "low": prem * 0.97,
            "close": prem, "volume": 1000.0,
        }, index=self.spy.index)


def test_build_training_set_shapes(cfg, synthetic_bars):
    X, y = build_training_set(cfg, synthetic_bars)
    assert len(X) == len(y) > 0
    assert X.isna().sum().sum() == 0
    assert "vix" in X.columns  # fixture supplies a real-shaped vix column


def test_build_snapshot_offline(cfg, synthetic_bars):
    snap = build_snapshot(cfg, synthetic_bars, model=None)
    assert snap.spy_price > 0
    assert 0 <= snap.ml_prob_up <= 1
    assert snap.regime.value in {"trend_up", "trend_down", "chop", "volatile"}


def test_backtest_runs_end_to_end_on_real_shaped_bars(cfg, synthetic_bars, monkeypatch):
    # Rules-only, independent of any model artifacts on disk.
    monkeypatch.setattr(backtest.DirectionalClassifier, "exists",
                        staticmethod(lambda *a, **k: False))
    # Loosen gates so the real-bars fill/exit walk actually executes.
    for k, v in {"ml_threshold_long": 0.5, "ml_threshold_short": 0.5, "min_rvol": 0.5,
                 "vwap_band_pct": 0.0}.items():
        cfg.signal._data[k] = v
        setattr(cfg.signal, k, v)

    poly = FakePolygon(synthetic_bars)
    result = backtest.run_backtest(cfg, days=1, verbose=False, poly=poly, bars=synthetic_bars)

    assert "report" in result
    assert result["report"]["total_trades"] >= 1          # trades really fired
    assert result["contracts_fetched"] >= 1               # real-contract resolution ran
    assert result["final_equity"] > 0
