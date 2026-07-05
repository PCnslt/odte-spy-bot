"""Walk-forward, out-of-sample backtest — the honest test for edge.

For each fold we train the directional model on a trailing window of REAL data, then trade the
NEXT (unseen) window with real option fills. Trades from every test window are concatenated into
a single out-of-sample record. A strategy that only looks good in-sample will look flat or
negative here — which is the entire point.

    python -m src.research.walkforward --days 180 --train 20 --test 5

Requires POLYGON_API_KEY and data within your plan's history window (~2 years on Starter).
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from ..backtest import run_backtest, ET
from ..data.data_pipeline import has_vix, load_bars
from ..data.polygon_options import PolygonOptions
from ..learning.evaluator import summarize
from ..signals.feature_engineering import build_features
from ..signals.labeling import make_labels
from ..signals.lightgbm_model import DirectionalClassifier
from ..utils.config import load_config
from ..utils.logger import get_logger

log = get_logger("walkforward")


def _trading_dates(index) -> list:
    return sorted(set(index.tz_convert(ET).date))


def run_walkforward(cfg, days: int = 180, train_win: int = 20, test_win: int = 5,
                    verbose: bool = True) -> dict:
    poly = PolygonOptions.from_config(cfg)
    bars = load_bars(cfg, days, download=False, poly=poly)
    include_vix = has_vix(bars)

    features = build_features(bars, include_vix=include_vix)
    mp = cfg.model_params
    labels, valid = make_labels(bars["close"], horizon_bars=mp["label"]["horizon_bars"],
                                threshold_pct=mp["label"]["threshold_pct"])
    et_dates = np.array(bars.index.tz_convert(ET).date)
    finite = features.notna().all(axis=1).to_numpy() & np.isfinite(features.to_numpy()).all(axis=1)
    base_mask = valid.to_numpy() & finite

    dates = _trading_dates(bars.index)
    log.info("Walk-forward over %d trading days (train=%d, test=%d).", len(dates),
             train_win, test_win)

    all_oos = []
    fold_rows = []
    start = train_win
    while start < len(dates):
        train_dates = set(dates[start - train_win:start])
        test_dates = set(dates[start:start + test_win])
        if not test_dates:
            break

        tr_mask = base_mask & np.isin(et_dates, list(train_dates))
        X, y = features[tr_mask], labels[tr_mask]
        if len(X) < 100 or y.nunique() < 2:
            start += test_win
            continue

        clf = DirectionalClassifier(params=mp["lightgbm"], feature_columns=list(features.columns))
        clf.train(X, y, valid_fraction=mp["train"]["valid_fraction"],
                  num_boost_round=mp["train"]["num_boost_round"],
                  early_stopping_rounds=mp["train"]["early_stopping_rounds"])

        res = run_backtest(cfg, bars=bars, model=clf, poly=poly, allow_dates=test_dates,
                           verbose=False)
        trades = res["trades"]
        all_oos.extend(trades)
        rep = res["report"]
        fold_rows.append({
            "test_from": min(test_dates), "test_to": max(test_dates),
            "trades": rep["total_trades"], "pnl": round(rep["total_pnl"], 2),
            "win": round(rep["win_rate"], 2),
        })
        start += test_win

    oos = summarize(all_oos)
    result = {"oos": oos.as_dict(), "folds": fold_rows, "n_folds": len(fold_rows),
              "n_trading_days": len(dates), "vix": include_vix}
    if verbose:
        print("\n=== Walk-forward OUT-OF-SAMPLE (real option fills) ===")
        print(f"{len(dates)} trading days, {len(fold_rows)} folds, "
              f"train={train_win}d/test={test_win}d, vix={'yes' if include_vix else 'no'}")
        for f in fold_rows:
            print(f"  {f['test_from']}..{f['test_to']}  trades={f['trades']:2d}  "
                  f"win={f['win']:.0%}  pnl=${f['pnl']:.2f}")
        print("-" * 60)
        print("OOS " + oos.pretty())
        print("(Every trade above was on data the model never saw during training.)")
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Walk-forward out-of-sample backtest")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--train", type=int, default=20, help="training window in trading days")
    p.add_argument("--test", type=int, default=5, help="test window in trading days")
    args = p.parse_args()
    run_walkforward(load_config(), days=args.days, train_win=args.train, test_win=args.test)


if __name__ == "__main__":
    main()
