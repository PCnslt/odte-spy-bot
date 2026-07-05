"""Training / retraining entry point. Used both manually and by the nightly GitHub Action.

    python -m src.learning.trainer --train --days 30
    python -m src.learning.trainer --retrain        # only replace model if it validates better
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..data.data_pipeline import build_training_set, has_vix, load_bars
from ..signals.feature_engineering import build_features
from ..signals.labeling import make_range_labels
from ..signals.lightgbm_model import DirectionalClassifier
from ..signals.range_model import RangeForecaster
from ..utils.config import load_config
from ..utils.logger import get_logger

log = get_logger("trainer")


def train(cfg, days: int, download: bool = True) -> DirectionalClassifier:
    bars = load_bars(cfg, days, download=download)
    X, y = build_training_set(cfg, bars)
    mp = cfg.model_params
    # Feature set is whatever the real data supports (VIX included only if entitled/available).
    clf = DirectionalClassifier(params=mp["lightgbm"], feature_columns=list(X.columns))
    clf.train(
        X, y,
        valid_fraction=mp["train"]["valid_fraction"],
        num_boost_round=mp["train"]["num_boost_round"],
        early_stopping_rounds=mp["train"]["early_stopping_rounds"],
    )
    return clf


def build_range_training_set(cfg, bars):
    """(X, y) for the range forecaster: features -> forward max excursion fraction."""
    include_vix = has_vix(bars)
    features = build_features(bars, include_vix=include_vix)
    horizon = cfg.intelligence.get("range_horizon_bars", 60)
    y, valid = make_range_labels(bars["high"], bars["low"], bars["close"],
                                 horizon_bars=horizon)
    mask = valid & features.notna().all(axis=1)
    return features[mask], y[mask]


def train_range(cfg, days: int, download: bool = False) -> RangeForecaster:
    bars = load_bars(cfg, days, download=download)
    X, y = build_range_training_set(cfg, bars)
    mp = cfg.model_params
    rf = RangeForecaster(params=mp["lightgbm"], feature_columns=list(X.columns))
    rf.train(X, y, valid_fraction=mp["train"]["valid_fraction"],
             num_boost_round=mp["train"]["num_boost_round"],
             early_stopping_rounds=mp["train"]["early_stopping_rounds"])
    return rf


def _validation_mae(rf: RangeForecaster, cfg, days: int) -> float:
    bars = load_bars(cfg, days, download=False)
    X, y = build_range_training_set(cfg, bars)
    split = int(len(X) * (1 - cfg.model_params["train"]["valid_fraction"]))
    return rf.mae(X.iloc[split:], y.iloc[split:])


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

    result = {"direction_updated": True, "range_updated": True}
    if args.retrain and DirectionalClassifier.exists(model_path, meta_path):
        new_ll = _validation_logloss(clf, cfg, days)
        old = DirectionalClassifier.load(model_path, meta_path)
        old_ll = _validation_logloss(old, cfg, days)
        log.info("Direction retrain check: new logloss=%.4f vs old=%.4f", new_ll, old_ll)
        if new_ll >= old_ll:
            log.info("New direction model not better; keeping existing.")
            result["direction_updated"] = False
    if result["direction_updated"]:
        clf.save(model_path, meta_path)
        log.info("Saved direction model to %s", model_path)
        result["top_features"] = list(clf.feature_importance())[:8]

    # --- range forecaster (the spread-seller's target) ---
    rpath, rmeta = cfg.model.range_path, cfg.model.range_meta_path
    rf = train_range(cfg, days, download=False)  # bars already cached above
    if args.retrain and RangeForecaster.exists(rpath, rmeta):
        new_mae = _validation_mae(rf, cfg, days)
        old_rf = RangeForecaster.load(rpath, rmeta)
        old_mae = _validation_mae(old_rf, cfg, days)
        log.info("Range retrain check: new MAE=%.6f vs old=%.6f", new_mae, old_mae)
        if new_mae >= old_mae:
            log.info("New range model not better; keeping existing.")
            result["range_updated"] = False
    if result["range_updated"]:
        rf.save(rpath, rmeta)
        log.info("Saved range model to %s", rpath)

    print(json.dumps(result))


if __name__ == "__main__":
    main()
