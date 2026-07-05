"""RangeForecaster: predicts the forward max excursion of SPY (fraction of spot).

This is the spread-seller's true target: "how far can price travel against me over the
hold window?" The prediction drives strike placement (short strike beyond the expected
range x safety) and the entry gate. LightGBM regression — for small tabular data it beats
transformer alternatives on accuracy, latency, and robustness, which is why the DeepSeek
suggestion to replace it with HF models was evaluated and rejected (see docs/AI_REVIEW.md).

Fail-safe: if no trained model exists, callers fall back to an ATR-based estimate.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from ..utils.logger import get_logger

log = get_logger("range_model")


class RangeForecaster:
    def __init__(self, params: Optional[dict] = None, feature_columns: Optional[list] = None):
        base = {"objective": "regression", "metric": "l1", "verbose": -1}
        self.params = {**base, **(params or {})}
        # Force regression objective regardless of shared classifier params.
        self.params["objective"] = "regression"
        self.params["metric"] = "l1"
        self.feature_columns = feature_columns or []
        self.model = None
        self.best_iteration = None

    def train(self, X: pd.DataFrame, y: pd.Series, valid_fraction: float = 0.2,
              num_boost_round: int = 1000, early_stopping_rounds: int = 50) -> "RangeForecaster":
        import lightgbm as lgb

        if not self.feature_columns:
            self.feature_columns = list(X.columns)
        X = X[self.feature_columns]
        split = int(len(X) * (1 - valid_fraction))
        if split < 10 or split >= len(X):
            raise ValueError(f"Not enough rows to train/validate (have {len(X)}).")
        train_set = lgb.Dataset(X.iloc[:split], label=y.iloc[:split])
        valid_set = lgb.Dataset(X.iloc[split:], label=y.iloc[split:], reference=train_set)
        self.model = lgb.train(
            self.params, train_set, num_boost_round=num_boost_round,
            valid_sets=[valid_set],
            callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False),
                       lgb.log_evaluation(0)],
        )
        self.best_iteration = self.model.best_iteration or num_boost_round
        log.info("Trained RangeForecaster: best_iteration=%s", self.best_iteration)
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("Range model not trained/loaded.")
        preds = self.model.predict(X[self.feature_columns], num_iteration=self.best_iteration)
        return np.clip(preds, 0.0, None)  # a range cannot be negative

    def predict_one(self, features: dict) -> float:
        row = pd.DataFrame([{c: features.get(c, 0.0) for c in self.feature_columns}])
        return float(self.predict(row)[0])

    def mae(self, X: pd.DataFrame, y: pd.Series) -> float:
        return float(np.abs(self.predict(X) - y.to_numpy()).mean())

    # --- persistence -----------------------------------------------------------
    def save(self, model_path: str | Path, meta_path: str | Path) -> None:
        if self.model is None:
            raise RuntimeError("Nothing to save.")
        Path(model_path).parent.mkdir(parents=True, exist_ok=True)
        self.model.save_model(str(model_path), num_iteration=self.best_iteration)
        Path(meta_path).write_text(json.dumps({
            "feature_columns": self.feature_columns,
            "best_iteration": self.best_iteration,
            "params": self.params,
        }, indent=2))

    @classmethod
    def load(cls, model_path: str | Path, meta_path: str | Path) -> "RangeForecaster":
        import lightgbm as lgb

        meta = json.loads(Path(meta_path).read_text())
        obj = cls(params=meta.get("params"), feature_columns=meta["feature_columns"])
        obj.model = lgb.Booster(model_file=str(model_path))
        obj.best_iteration = meta.get("best_iteration")
        return obj

    @staticmethod
    def exists(model_path: str | Path, meta_path: str | Path) -> bool:
        return Path(model_path).exists() and Path(meta_path).exists()


# --- pure helpers (unit-tested) --------------------------------------------------
def atr_range_estimate(atr_5min: float, spot: float, horizon_bars: int) -> float:
    """Fallback expected range (fraction of spot) when no model exists: ATR-scaled
    sqrt-of-time diffusion. Deliberately conservative."""
    if spot <= 0:
        return 0.0
    per_bar = max(atr_5min, 0.0) / spot
    return per_bar * float(np.sqrt(max(horizon_bars, 1)))


def dynamic_short_otm(base_otm: float, range_pred: float, safety_mult: float,
                      max_otm: float) -> Optional[float]:
    """Short-strike distance: beyond the expected range x safety, floored at the static
    base, capped at max_otm. Returns None when the required distance exceeds the cap —
    i.e., the forecast says no safe strike exists close enough to earn a credit."""
    needed = max(base_otm, range_pred * safety_mult)
    if needed > max_otm:
        return None
    return needed
