"""CostMetaLabeler — a SHADOW classifier of EXECUTION quality (never direction, never P&L).

The evidence is unambiguous: entries carry zero information and costs (~$6.36/trade) dominate a
+$3.46/trade gross engine. The only rational survivor of the "profitability engine" is therefore
a **cost-avoidance** lever: predict, from cost-context features observable AT ENTRY, whether this
trade's fill will be expensive relative to the credit — so a future (pre-registered) gate can
skip spreads the market is about to eat.

Honesty guards baked in:
  * Features are cost-context ONLY — leg half-spreads, time-in/-to-session, realized vol
    magnitude, credit, width, dealer-gamma posture. NO returns, NO RSI/MACD/breakout, NO ML
    directional prob. The model is structurally incapable of learning to time the market.
  * Its strongest feature (leg half-spreads) is ALREADY a hard gate (`liquidity_ok`), so its
    only headroom is interactions that gate misses (e.g. spread × time-of-day × gamma). Don't
    oversell it.
  * Fail-closed: no model / too little data -> `predict_one` returns 0.5 (neutral). It ships
    SHADOW — logged, inert — until H10 clears a holdout.

Trains on the live TradeLog only (never the reserved holdout), once >=100 closed trades with
fill data exist. Same save/load pattern as the other models.

    python -m src.signals.cost_meta_labeler --train --db trades.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from ..utils.logger import get_logger

log = get_logger("cost_meta_labeler")

# Cost-context features ONLY. Derived at both train (from trades.db) and predict (from the live
# snapshot) so the columns match. Nothing here encodes price direction.
COST_FEATURES = [
    "short_half_spread",   # (short_ask - short_bid)/2, real IBKR quote
    "long_half_spread",    # (long_ask  - long_bid )/2
    "half_spread_frac",    # (short+long half-spreads) / credit  — the liquidity-gate ratio
    "minutes_into_session",
    "minutes_to_close",
    "rv_annual",           # realized-vol MAGNITUDE (non-directional)
    "credit",
    "width",
    "gex_net",             # dealer gamma posture (cost-relevant: high |gamma| -> wilder fills)
    "gamma_wall_dist",     # |spot - gamma_wall| / spot
]

# BAD_FILL = adverse round-trip slippage ate more than this fraction of the credit. With ~$2.60
# commission already charged, losing half the credit to slippage on top typically flips the
# trade net-negative. Configurable; recalibrate once a real slippage distribution exists.
BAD_FILL_THRESHOLD_FRAC = 0.5
MIN_TRAIN_TRADES = 100


class CostMetaLabeler:
    def __init__(self, params: Optional[dict] = None, feature_columns: Optional[list] = None):
        base = {"objective": "binary", "metric": "binary_logloss", "verbose": -1,
                "num_leaves": 15, "learning_rate": 0.05, "min_data_in_leaf": 20}
        self.params = {**base, **(params or {})}
        self.params["objective"] = "binary"
        self.feature_columns = feature_columns or list(COST_FEATURES)
        self.model = None
        self.best_iteration = None

    def train(self, X: pd.DataFrame, y: pd.Series, valid_fraction: float = 0.2,
              num_boost_round: int = 400, early_stopping_rounds: int = 40) -> "CostMetaLabeler":
        import lightgbm as lgb

        X = X[self.feature_columns]
        split = int(len(X) * (1 - valid_fraction))
        if split < 10 or split >= len(X):
            raise ValueError(f"Not enough rows to train/validate (have {len(X)}).")
        if y.nunique() < 2:
            raise ValueError("Cost labels are single-class; cannot train a BAD_FILL model yet.")
        # Time-series split: validate on the most recent slice, never shuffle.
        train_set = lgb.Dataset(X.iloc[:split], label=y.iloc[:split])
        valid_set = lgb.Dataset(X.iloc[split:], label=y.iloc[split:], reference=train_set)
        self.model = lgb.train(
            self.params, train_set, num_boost_round=num_boost_round, valid_sets=[valid_set],
            callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False),
                       lgb.log_evaluation(0)])
        self.best_iteration = self.model.best_iteration or num_boost_round
        log.info("Trained CostMetaLabeler: best_iteration=%s, n=%d", self.best_iteration, len(X))
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            return np.full(len(X), 0.5)
        return self.model.predict(X[self.feature_columns], num_iteration=self.best_iteration)

    def predict_one(self, features: dict) -> float:
        """P(BAD_FILL). Fail-closed to 0.5 when no model is loaded. None features -> NaN
        (LightGBM handles missing values natively)."""
        if self.model is None:
            return 0.5
        row = pd.DataFrame([{c: (features.get(c) if features.get(c) is not None else np.nan)
                             for c in self.feature_columns}])
        return float(self.predict_proba(row)[0])

    def feature_importance(self) -> dict:
        if self.model is None:
            return {}
        imp = self.model.feature_importance(importance_type="gain")
        return dict(sorted(zip(self.feature_columns, imp), key=lambda kv: -kv[1]))

    def save(self, model_path: str | Path, meta_path: str | Path) -> None:
        if self.model is None:
            raise RuntimeError("Nothing to save; model not trained.")
        Path(model_path).parent.mkdir(parents=True, exist_ok=True)
        self.model.save_model(str(model_path), num_iteration=self.best_iteration)
        Path(meta_path).write_text(json.dumps({
            "feature_columns": self.feature_columns, "best_iteration": self.best_iteration,
            "params": self.params, "threshold_frac": BAD_FILL_THRESHOLD_FRAC}, indent=2))

    @classmethod
    def load(cls, model_path: str | Path, meta_path: str | Path) -> "CostMetaLabeler":
        import lightgbm as lgb

        meta = json.loads(Path(meta_path).read_text())
        obj = cls(params=meta.get("params"), feature_columns=meta["feature_columns"])
        obj.model = lgb.Booster(model_file=str(model_path))
        obj.best_iteration = meta.get("best_iteration")
        return obj

    @staticmethod
    def exists(model_path: str | Path, meta_path: str | Path) -> bool:
        return Path(model_path).exists() and Path(meta_path).exists()


# --- training-set construction from the live TradeLog -----------------------------------------
def cost_features_from_row(r: dict) -> dict:
    """Rebuild the COST_FEATURES from a stored trades.db row (train side)."""
    sh, lh = r.get("short_half_spread"), r.get("long_half_spread")
    credit = r.get("credit_est")
    frac = ((sh + lh) / credit) if (sh is not None and lh is not None and credit) else None
    spot, wall = r.get("spot"), r.get("gamma_wall")
    wall_dist = (abs(spot - wall) / spot) if (spot and wall) else None
    return {"short_half_spread": sh, "long_half_spread": lh, "half_spread_frac": frac,
            "minutes_into_session": r.get("minutes_into_session"),
            "minutes_to_close": r.get("minutes_to_close"), "rv_annual": r.get("rv_annual"),
            "credit": credit, "width": r.get("width"), "gex_net": r.get("gex_net"),
            "gamma_wall_dist": wall_dist}


def build_cost_training_set(db_path: str, threshold_frac: float = BAD_FILL_THRESHOLD_FRAC
                            ) -> tuple[pd.DataFrame, pd.Series]:
    """(X, y) where y = BAD_FILL. Only closed trades with both slippage legs and a credit."""
    if not Path(db_path).exists():
        return pd.DataFrame(columns=COST_FEATURES), pd.Series(dtype=int)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE closed_at IS NOT NULL AND credit_est IS NOT NULL "
            "AND entry_slippage IS NOT NULL AND exit_slippage IS NOT NULL ORDER BY opened_at")]
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    feats, labels = [], []
    for r in rows:
        if not r["credit_est"]:
            continue
        total_slip = (r["entry_slippage"] or 0.0) + (r["exit_slippage"] or 0.0)
        labels.append(1 if total_slip > threshold_frac * r["credit_est"] else 0)
        feats.append(cost_features_from_row(r))
    X = pd.DataFrame(feats, columns=COST_FEATURES) if feats else pd.DataFrame(columns=COST_FEATURES)
    return X, pd.Series(labels, name="bad_fill", dtype=int)


def train_from_db(db_path: str, model_path: str, meta_path: str,
                  min_trades: int = MIN_TRAIN_TRADES) -> Optional[CostMetaLabeler]:
    """Train + save iff enough labelled trades with both classes exist. Else no-op (returns
    None) so the nightly/EOD hook never crashes while the book is young."""
    X, y = build_cost_training_set(db_path)
    if len(X) < min_trades:
        log.info("CostMetaLabeler: only %d labelled trades (<%d) — skipping train (stays 0.5).",
                 len(X), min_trades)
        return None
    if y.nunique() < 2:
        log.info("CostMetaLabeler: labels single-class at n=%d — skipping train.", len(X))
        return None
    clf = CostMetaLabeler().train(X, y)
    clf.save(model_path, meta_path)
    log.info("CostMetaLabeler saved to %s (BAD_FILL rate %.1f%%).", model_path, 100 * y.mean())
    return clf


def main() -> None:
    p = argparse.ArgumentParser(description="Train the shadow cost-quality meta-labeler")
    p.add_argument("--train", action="store_true")
    p.add_argument("--db", default="trades.db")
    p.add_argument("--model", default="models/cost_meta_labeler.txt")
    p.add_argument("--meta", default="models/cost_meta_labeler_meta.json")
    p.add_argument("--min", type=int, default=MIN_TRAIN_TRADES)
    args = p.parse_args()
    if args.train:
        clf = train_from_db(args.db, args.model, args.meta, min_trades=args.min)
        print(json.dumps({"trained": clf is not None,
                          "top_features": list(clf.feature_importance())[:5] if clf else []}))


if __name__ == "__main__":
    main()
