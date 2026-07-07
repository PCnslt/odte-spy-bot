"""Tests for the shadow CostMetaLabeler — it must learn EXECUTION cost, never direction."""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from src.signals.cost_meta_labeler import (COST_FEATURES, CostMetaLabeler,
                                           build_cost_training_set, train_from_db)
from src.utils.trade_log import TradeLog


def _synth_cost_frame(n=400, seed=0):
    """Trades whose BAD_FILL is driven by half_spread_frac ONLY, plus a decoy 'fake_dir'
    column (a stand-in for a directional signal) that is pure noise."""
    rng = np.random.default_rng(seed)
    frac = rng.uniform(0.05, 0.6, n)
    credit = rng.uniform(0.2, 0.8, n)
    short_hs = frac * credit * rng.uniform(0.4, 0.6, n)
    long_hs = frac * credit - short_hs
    y = ((frac > 0.30).astype(int) ^ (rng.random(n) < 0.05).astype(int))  # 5% label noise
    X = pd.DataFrame({
        "short_half_spread": short_hs, "long_half_spread": long_hs, "half_spread_frac": frac,
        "minutes_into_session": rng.uniform(0, 390, n), "minutes_to_close": rng.uniform(0, 390, n),
        "rv_annual": rng.uniform(0.05, 0.3, n), "credit": credit,
        "width": rng.choice([5.0, 10.0], n), "gex_net": rng.normal(0, 1e9, n),
        "gamma_wall_dist": rng.uniform(0, 0.02, n),
    })[COST_FEATURES]
    fake_dir = rng.random(n)                       # NOT a feature; must not leak into preds
    return X, pd.Series(y, name="bad_fill"), fake_dir


def test_label_from_slippage_vs_credit(tmp_path):
    db = tmp_path / "t.db"
    TradeLog(str(db)).close()
    conn = sqlite3.connect(str(db))
    # credit 0.40 -> BAD_FILL if total slip > 0.20. Row A good (0.10), Row B bad (0.30).
    for slip, credit in [((0.05, 0.05), 0.40), ((0.15, 0.15), 0.40)]:
        conn.execute(
            "INSERT INTO trades (opened_at, closed_at, kind, credit_est, entry_slippage,"
            " exit_slippage, short_half_spread, long_half_spread, minutes_into_session,"
            " minutes_to_close, rv_annual, width, gex_net, spot, gamma_wall) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-07-06T10:00:00", "x", "bull_put", credit, slip[0], slip[1],
             0.03, 0.02, 30.0, 360.0, 0.15, 5.0, 1e9, 744.0, 745.0))
    conn.commit(); conn.close()
    X, y = build_cost_training_set(str(db))
    assert list(y) == [0, 1]
    assert list(X.columns) == COST_FEATURES and len(X) == 2


def test_learns_spread_not_time():
    X, y, _ = _synth_cost_frame()
    clf = CostMetaLabeler().train(X, y)
    imp = list(clf.feature_importance())
    # A half-spread feature must dominate; time-of-day must NOT be the top driver.
    assert imp[0] in ("half_spread_frac", "short_half_spread", "long_half_spread")
    # Separation: high-frac rows score higher P(BAD_FILL) than low-frac rows.
    hi = clf.predict_proba(X[X["half_spread_frac"] > 0.4])
    lo = clf.predict_proba(X[X["half_spread_frac"] < 0.15])
    assert hi.mean() > lo.mean() + 0.2


def test_predictions_uncorrelated_with_direction():
    X, y, fake_dir = _synth_cost_frame(seed=1)
    clf = CostMetaLabeler().train(X, y)
    preds = clf.predict_proba(X)
    r = float(np.corrcoef(preds, fake_dir)[0, 1])
    assert abs(r) < 0.2                              # not accidentally learning "direction"
    assert "ml_prob" not in clf.feature_columns      # structurally cannot see direction


def test_save_load_roundtrip(tmp_path):
    X, y, _ = _synth_cost_frame(seed=2)
    clf = CostMetaLabeler().train(X, y)
    mp, mm = tmp_path / "c.txt", tmp_path / "c.json"
    clf.save(mp, mm)
    assert CostMetaLabeler.exists(mp, mm)
    clf2 = CostMetaLabeler.load(mp, mm)
    feat = X.iloc[10].to_dict()
    assert abs(clf.predict_one(feat) - clf2.predict_one(feat)) < 1e-9


def test_fail_closed_to_half():
    clf = CostMetaLabeler()                          # never trained
    assert clf.predict_one({"half_spread_frac": 0.5}) == 0.5
    assert not CostMetaLabeler.exists("nope.txt", "nope.json")
    # Missing feature values must not crash (None -> NaN, handled by LightGBM once trained).
    X, y, _ = _synth_cost_frame(seed=3)
    trained = CostMetaLabeler().train(X, y)
    p = trained.predict_one({"half_spread_frac": None, "credit": 0.4})  # sparse dict
    assert 0.0 <= p <= 1.0


def test_train_from_db_gate(tmp_path):
    db = tmp_path / "t.db"
    TradeLog(str(db)).close()
    conn = sqlite3.connect(str(db))
    # 150 rows, BAD_FILL driven by half-spread, both classes present.
    rng = np.random.default_rng(7)
    for i in range(150):
        frac = rng.uniform(0.05, 0.6)
        credit = 0.4
        slip = frac * credit * 1.2 if frac > 0.3 else frac * credit * 0.3
        conn.execute(
            "INSERT INTO trades (opened_at, closed_at, kind, credit_est, entry_slippage,"
            " exit_slippage, short_half_spread, long_half_spread, minutes_into_session,"
            " minutes_to_close, rv_annual, width, gex_net, spot, gamma_wall) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"2026-07-{(i % 27) + 1:02d}T10:00:00", "x", "bull_put", credit, slip / 2, slip / 2,
             frac * credit / 2, frac * credit / 2, 30.0, 360.0, 0.15, 5.0, 1e9, 744.0, 745.0))
    conn.commit(); conn.close()
    # Below the gate -> no model.
    assert train_from_db(str(db), str(tmp_path / "a.txt"), str(tmp_path / "a.json"),
                         min_trades=500) is None
    # Above the gate -> trains + saves.
    clf = train_from_db(str(db), str(tmp_path / "b.txt"), str(tmp_path / "b.json"),
                        min_trades=100)
    assert clf is not None and CostMetaLabeler.exists(tmp_path / "b.txt", tmp_path / "b.json")


def test_shadow_feature_dict_contract():
    """The exact dict main.py builds (with some None values) must predict without error."""
    X, y, _ = _synth_cost_frame(seed=4)
    clf = CostMetaLabeler().train(X, y)
    cost_feats = {"short_half_spread": 0.03, "long_half_spread": 0.02, "half_spread_frac": 0.12,
                  "minutes_into_session": 45.0, "minutes_to_close": 350.0, "rv_annual": 0.14,
                  "credit": 0.40, "width": 5.0, "gex_net": None, "gamma_wall_dist": None}
    assert 0.0 <= clf.predict_one(cost_feats) <= 1.0
