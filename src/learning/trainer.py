"""Training / retraining entry point. Used both manually and by the nightly GitHub Action.

    python -m src.learning.trainer --train --days 30
    python -m src.learning.trainer --retrain        # only replace model if it validates better
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..data.data_pipeline import build_training_set, load_bars
from ..signals.lightgbm_model import DirectionalClassifier
from ..utils.config import load_config
from ..utils.logger import get_logger

log = get_logger("trainer")


def train(cfg, days: int, download: bool = True) -> DirectionalClassifier:
    bars = load_bars(cfg, days, download=download)
    X, y = build_training_set(cfg, bars)
    mp = cfg.model_params
    clf = DirectionalClassifier(params=mp["lightgbm"])
    clf.train(
        X, y,
        valid_fraction=mp["train"]["valid_fraction"],
        num_boost_round=mp["train"]["num_boost_round"],
        early_stopping_rounds=mp["train"]["early_stopping_rounds"],
    )
    return clf


def _validation_logloss(clf: DirectionalClassifier, cfg, days: int) -> float:
    """Quick holdout logloss to decide whether a retrain is an improvement."""
    import numpy as np

    bars = load_bars(cfg, days, download=False)
    X, y = build_training_set(cfg, bars)
    split = int(len(X) * (1 - cfg.model_params["train"]["valid_fraction"]))
    proba = clf.predict_proba(X.iloc[split:])
    yv = y.iloc[split:].to_numpy()
    eps = 1e-15
    proba = proba.clip(eps, 1 - eps)
    return float(-(yv * np.log(proba) + (1 - yv) * np.log(1 - proba)).mean())


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the directional model")
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--retrain", action="store_true",
                        help="only overwrite the saved model if the new one validates better")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--no-download", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    days = args.days or cfg.model_params["train"]["lookback_days"]
    model_path = cfg.model.path
    meta_path = cfg.model.meta_path

    clf = train(cfg, days, download=not args.no_download)

    if args.retrain and DirectionalClassifier.exists(model_path, meta_path):
        new_ll = _validation_logloss(clf, cfg, days)
        old = DirectionalClassifier.load(model_path, meta_path)
        old_ll = _validation_logloss(old, cfg, days)
        log.info("Retrain check: new logloss=%.4f vs old=%.4f", new_ll, old_ll)
        if new_ll >= old_ll:
            log.info("New model not better; keeping existing model.")
            print(json.dumps({"updated": False, "new_logloss": new_ll, "old_logloss": old_ll}))
            return

    clf.save(model_path, meta_path)
    log.info("Saved model to %s", model_path)
    print(json.dumps({"updated": True, "top_features": list(clf.feature_importance())[:8]}))


if __name__ == "__main__":
    main()
