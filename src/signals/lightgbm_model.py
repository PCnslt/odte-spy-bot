"""LightGBM directional classifier. Predicts P(SPY moves up over the label horizon)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from ..utils.logger import get_logger
from .feature_engineering import FEATURE_COLUMNS

log = get_logger("lightgbm_model")


class DirectionalClassifier:
    def __init__(self, params: Optional[dict] = None, feature_columns: Optional[list] = None):
        self.params = params or {}
        self.feature_columns = feature_columns or list(FEATURE_COLUMNS)
        self.model = None
        self.best_iteration = None

    def train(self, X: pd.DataFrame, y: pd.Series, valid_fraction: float = 0.2,
              num_boost_round: int = 1000, early_stopping_rounds: int = 50) -> "DirectionalClassifier":
        import lightgbm as lgb

        X = X[self.feature_columns]
        split = int(len(X) * (1 - valid_fraction))
        if split < 10 or split >= len(X):
            raise ValueError(f"Not enough rows to train/validate (have {len(X)}).")

        # Walk-forward: validate on the most recent slice, never shuffle time series.
        train_set = lgb.Dataset(X.iloc[:split], label=y.iloc[:split])
        valid_set = lgb.Dataset(X.iloc[split:], label=y.iloc[split:], reference=train_set)

        params = {"objective": "binary", "metric": "binary_logloss", "verbose": -1, **self.params}
        callbacks = [
            lgb.early_stopping(early_stopping_rounds, verbose=False),
            lgb.log_evaluation(0),
        ]
        self.model = lgb.train(
            params, train_set, num_boost_round=num_boost_round,
            valid_sets=[valid_set], callbacks=callbacks,
        )
        self.best_iteration = self.model.best_iteration or num_boost_round
        log.info("Trained LightGBM: best_iteration=%s, features=%d",
                 self.best_iteration, len(self.feature_columns))
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("Model not trained/loaded.")
        X = X[self.feature_columns]
        return self.model.predict(X, num_iteration=self.best_iteration)

    def predict_one(self, features: dict) -> float:
        row = pd.DataFrame([{c: features.get(c, 0.0) for c in self.feature_columns}])
        return float(self.predict_proba(row)[0])

    def feature_importance(self) -> dict:
        if self.model is None:
            return {}
        imp = self.model.feature_importance(importance_type="gain")
        return dict(sorted(zip(self.feature_columns, imp), key=lambda kv: -kv[1]))

    # --- persistence -----------------------------------------------------------
    def save(self, model_path: str | Path, meta_path: str | Path) -> None:
        if self.model is None:
            raise RuntimeError("Nothing to save; model not trained.")
        Path(model_path).parent.mkdir(parents=True, exist_ok=True)
        self.model.save_model(str(model_path), num_iteration=self.best_iteration)
        Path(meta_path).write_text(json.dumps({
            "feature_columns": self.feature_columns,
            "best_iteration": self.best_iteration,
            "params": self.params,
        }, indent=2))

    @classmethod
    def load(cls, model_path: str | Path, meta_path: str | Path) -> "DirectionalClassifier":
        import lightgbm as lgb

        meta = json.loads(Path(meta_path).read_text())
        obj = cls(params=meta.get("params"), feature_columns=meta["feature_columns"])
        obj.model = lgb.Booster(model_file=str(model_path))
        obj.best_iteration = meta.get("best_iteration")
        return obj

    @staticmethod
    def exists(model_path: str | Path, meta_path: str | Path) -> bool:
        return Path(model_path).exists() and Path(meta_path).exists()
