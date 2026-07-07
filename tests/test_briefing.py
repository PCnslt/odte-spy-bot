"""Tests for the plain-English operator briefing (sqlite + monitor only)."""
from __future__ import annotations

import sqlite3

from src.briefing import briefing, panel_markdown
from src.utils.trade_log import TradeLog


def _seed(path, pnls, slips=None):
    TradeLog(str(path)).close()
    conn = sqlite3.connect(str(path))
    for i, pnl in enumerate(pnls):
        es, xs = (slips[i] if slips else (None, None))
        conn.execute(
            "INSERT INTO trades (opened_at, closed_at, kind, pnl, entry_slippage, exit_slippage)"
            " VALUES (?,?,?,?,?,?)",
            (f"2026-07-{(i % 27) + 1:02d}T10:00:00", "x", "bull_put", pnl, es, xs))
    conn.commit(); conn.close()


def test_no_trades_says_armed(tmp_path):
    b = briefing(str(tmp_path / "none.db"))
    assert b["emoji"] == "🟢" and b["n"] == 0
    assert "hasn't traded" in b["headline"]
    assert "BOTTOM LINE" in b["text"] and "WHAT TO DO" in b["text"]


def test_kill_watch_says_stop(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, [-8.0] * 100, slips=[(0.20, 0.20)] * 100)   # clearly-negative at n=100
    b = briefing(str(db))
    assert b["emoji"] == "🟠" and b["flag"] == "KILL_WATCH"
    assert "stop" in b["action"].lower() or "pivot" in b["action"].lower()


def test_healthy_positive_book(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, [6.0] * 40, slips=[(0.02, 0.02)] * 40)
    b = briefing(str(db))
    assert b["emoji"] == "🟢"
    assert "GOOD case" in b["text"]                         # near-mid fills flagged as good


def test_expensive_fills_flagged(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, [1.0] * 30, slips=[(0.20, 0.20)] * 30)        # total 0.40 slip -> pessimistic
    b = briefing(str(db))
    assert "PESSIMISTIC" in b["text"] and b["emoji"] == "🟠"


def test_panel_renders(tmp_path):
    b = briefing(str(tmp_path / "none.db"))
    md = "\n".join(panel_markdown(b))
    assert "Bottom line" in md and "What to do" in md
