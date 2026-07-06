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
from ..signals.labeling import make_breach_labels, make_range_labels
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


_METRICS_HISTORY = Path("models/metrics_history.json")


def _load_hist() -> dict:
    if _METRICS_HISTORY.exists():
        try:
            return json.loads(_METRICS_HISTORY.read_text())
        except Exception:
            return {}
    return {}


def _record_metric(key: str, value: float) -> None:
    hist = _load_hist()
    hist.setdefault(key, []).append(round(float(value), 6))
    hist[key] = hist[key][-30:]
    _METRICS_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    _METRICS_HISTORY.write_text(json.dumps(hist, indent=2))


def _sanity_ok(key: str, value: float, mult: float = 2.0) -> bool:
    """Absolute sanity floor: reject a candidate whose validation metric is > mult x the
    median of recent deployed metrics. Catches corrupted-data retrains that the RELATIVE
    only-if-better gate cannot (both models score badly on garbage; one still 'wins')."""
    import statistics

    hist = _load_hist().get(key, [])
    if len(hist) < 3:
        return True  # not enough history to judge
    med = statistics.median(hist[-5:])
    if med <= 0:
        return True
    if value > mult * med:
        log.error("SANITY REJECT %s: %.6f > %.1fx median(%.6f) — data corruption likely; "
                  "keeping deployed model.", key, value, mult, med)
        return False
    return True


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


def build_breach_training_sets(cfg, bars):
    """Two (X, y) sets for the EV gate: P(down-breach) and P(up-breach) of the static
    short-strike distance within the breach horizon."""
    include_vix = has_vix(bars)
    features = build_features(bars, include_vix=include_vix)
    dn, up, valid = make_breach_labels(
        bars["high"], bars["low"], bars["close"],
        horizon_bars=cfg.intelligence.get("breach_horizon_bars", 120),
        threshold_pct=cfg.spread.short_otm_pct)
    mask = valid & features.notna().all(axis=1)
    X = features[mask]
    return X, dn[mask], up[mask]


def train_breach(cfg, days: int) -> tuple[DirectionalClassifier, DirectionalClassifier]:
    bars = load_bars(cfg, days, download=False)
    X, ydn, yup = build_breach_training_sets(cfg, bars)
    mp = cfg.model_params
    models = []
    for y in (ydn, yup):
        clf = DirectionalClassifier(params=mp["lightgbm"], feature_columns=list(X.columns))
        clf.train(X, y, valid_fraction=mp["train"]["valid_fraction"],
                  num_boost_round=mp["train"]["num_boost_round"],
                  early_stopping_rounds=mp["train"]["early_stopping_rounds"])
        models.append(clf)
    return models[0], models[1]


def _breach_logloss(clf: DirectionalClassifier, cfg, days: int, side: str) -> float:
    import numpy as np

    bars = load_bars(cfg, days, download=False)
    X, ydn, yup = build_breach_training_sets(cfg, bars)
    y = ydn if side == "dn" else yup
    split = int(len(X) * (1 - cfg.model_params["train"]["valid_fraction"]))
    proba = clf.predict_proba(X.iloc[split:]).clip(1e-15, 1 - 1e-15)
    yv = y.iloc[split:].to_numpy()
    return float(-(yv * np.log(proba) + (1 - yv) * np.log(1 - proba)).mean())


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

    # Data sanity: a corrupted/partial download must abort the whole retrain, not train on it.
    bars = load_bars(cfg, days, download=not args.no_download)
    if len(bars) < 1000:
        raise SystemExit(f"ABORT retrain: only {len(bars)} bars for {days} days — "
                         "data feed problem; refusing to train on garbage.")

    clf = train(cfg, days, download=False)

    result = {"direction_updated": True, "range_updated": True}
    new_ll = _validation_logloss(clf, cfg, days)
    if not _sanity_ok("direction_logloss", new_ll):
        result["direction_updated"] = False
    elif args.retrain and DirectionalClassifier.exists(model_path, meta_path):
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
        _record_metric("direction_logloss", new_ll)

    # --- range forecaster (the spread-seller's target) ---
    rpath, rmeta = cfg.model.range_path, cfg.model.range_meta_path
    rf = train_range(cfg, days, download=False)  # bars already cached above
    new_mae = _validation_mae(rf, cfg, days)
    if not _sanity_ok("range_mae", new_mae):
        result["range_updated"] = False
    elif args.retrain and RangeForecaster.exists(rpath, rmeta):
        old_rf = RangeForecaster.load(rpath, rmeta)
        old_mae = _validation_mae(old_rf, cfg, days)
        log.info("Range retrain check: new MAE=%.6f vs old=%.6f", new_mae, old_mae)
        if new_mae >= old_mae:
            log.info("New range model not better; keeping existing.")
            result["range_updated"] = False
    if result["range_updated"]:
        rf.save(rpath, rmeta)
        log.info("Saved range model to %s", rpath)
        _record_metric("range_mae", new_mae)

    # --- breach models (EV / premium-richness gate) ---
    bdn, bup = train_breach(cfg, days)
    for side, bclf in (("dn", bdn), ("up", bup)):
        bpath = cfg.model.get(f"breach_{side}_path")
        bmeta = cfg.model.get(f"breach_{side}_meta_path")
        key = f"breach_{side}_updated"
        result[key] = True
        b_ll = _breach_logloss(bclf, cfg, days, side)
        if not _sanity_ok(f"breach_{side}_logloss", b_ll):
            result[key] = False
        elif args.retrain and DirectionalClassifier.exists(bpath, bmeta):
            old_ll = _breach_logloss(DirectionalClassifier.load(bpath, bmeta), cfg, days, side)
            log.info("Breach-%s retrain check: new=%.4f vs old=%.4f", side, b_ll, old_ll)
            if b_ll >= old_ll:
                result[key] = False
        if result[key]:
            bclf.save(bpath, bmeta)
            log.info("Saved breach-%s model to %s", side, bpath)
            _record_metric(f"breach_{side}_logloss", b_ll)

    print(json.dumps(result))


if __name__ == "__main__":
    main()
