"""Tests for the strategy-death-spiral monitor (numpy + sqlite only; no lightgbm/ib_insync)."""
from __future__ import annotations

import sqlite3

from src.monitor import (KILL_N, WATCH_N, bootstrap_ci, death_spiral_check,
                         panel_markdown)
from src.utils.trade_log import TradeLog


def _seed_db(path, pnls, dates=None, slippage=None):
    """Insert closed trades straight into the real TradeLog schema (fast + deterministic)."""
    TradeLog(str(path)).close()  # create schema + migrations
    conn = sqlite3.connect(str(path))
    for i, pnl in enumerate(pnls):
        day = dates[i] if dates else f"2026-07-{(i % 27) + 1:02d}"
        es, xs = (slippage[i] if slippage else (None, None))
        conn.execute(
            "INSERT INTO trades (opened_at, closed_at, kind, pnl, entry_slippage,"
            " exit_slippage) VALUES (?,?,?,?,?,?)",
            (f"{day}T10:00:00", f"{day}T11:00:00", "bull_put", pnl, es, xs))
    conn.commit()
    conn.close()


def test_bootstrap_ci_directional_and_deterministic():
    lo_p, mean_p, hi_p = bootstrap_ci([5.0] * 50 + [3.0] * 50)
    assert lo_p > 0 and hi_p > 0 and 3.0 < mean_p < 5.0
    lo_n, _, hi_n = bootstrap_ci([-10.0] * 60 + [-2.0] * 60)
    assert hi_n < 0                                   # all-negative -> CI upper below 0
    # Deterministic: identical inputs (seeded) -> identical CI.
    assert bootstrap_ci([1.0, -1.0, 2.0, -3.0]) == bootstrap_ci([1.0, -1.0, 2.0, -3.0])
    # Degenerate n<2 collapses to the point estimate.
    assert bootstrap_ci([4.0]) == (4.0, 4.0, 4.0)
    assert bootstrap_ci([]) == (0.0, 0.0, 0.0)


def test_insufficient_below_floor(tmp_path):
    db = tmp_path / "t.db"
    _seed_db(db, [-5.0] * 10)
    st = death_spiral_check(str(db))
    assert st["flag"] == "INSUFFICIENT" and st["n"] == 10


def test_kill_watch_fires_early(tmp_path):
    db = tmp_path / "t.db"
    _seed_db(db, [-8.0] * WATCH_N)                    # clearly-negative book at n=100
    st = death_spiral_check(str(db))
    assert st["flag"] == "KILL_WATCH"
    assert st["ci_hi"] < 0 and st["n"] == WATCH_N
    assert "EARLY WARNING" in st["reason"]


def test_healthy_when_ci_upper_positive(tmp_path):
    db = tmp_path / "t.db"
    _seed_db(db, [6.0] * 80 + [4.0] * 40)            # positive book, n=120
    st = death_spiral_check(str(db))
    assert st["flag"] == "HEALTHY" and st["ci_hi"] > 0


def test_retire_at_hard_kill_n(tmp_path):
    db = tmp_path / "t.db"
    _seed_db(db, [-9.0] * KILL_N)
    st = death_spiral_check(str(db))
    assert st["flag"] == "RETIRE" and st["n"] >= KILL_N


def test_consecutive_losing_sessions_and_slippage(tmp_path):
    db = tmp_path / "t.db"
    # 3 winning days then 2 losing days (most recent) -> trailing streak = 2.
    dates = (["2026-06-01"] * 10 + ["2026-06-02"] * 10 + ["2026-06-03"] * 10
             + ["2026-06-04"] * 10 + ["2026-06-05"] * 10)
    pnls = [5.0] * 30 + [-5.0] * 20
    slip = [(0.02, 0.03)] * 50
    _seed_db(db, pnls, dates=dates, slippage=slip)
    st = death_spiral_check(str(db))
    assert st["consec_losing_sessions"] == 2
    assert abs(st["mean_slippage"] - 0.05) < 1e-9     # 0.02 + 0.03
    assert "|" in "\n".join(panel_markdown(st))        # renders without error
