"""Tests for per-day session history (trades + log + SPY-range summary)."""
from __future__ import annotations

import sqlite3

from src.session_log import read_sessions, record_session
from src.utils.trade_log import TradeLog


def _seed(tmp_path):
    db = tmp_path / "t.db"
    TradeLog(str(db)).close()
    c = sqlite3.connect(str(db))
    c.execute("INSERT INTO trades(opened_at,closed_at,kind,pnl) VALUES(?,?,?,?)",
              ("2026-07-08T10:00:00", "2026-07-08T11:00:00", "bull_put", 12.5))
    c.execute("INSERT INTO trades(opened_at,kind) VALUES(?,?)",
              ("2026-07-08T12:00:00", "bear_call"))            # still open
    c.commit(); c.close()
    logs = tmp_path / "logs"; logs.mkdir()
    (logs / "daily_20260708.log").write_text("Bot starting\nANOMALY ['PRICE_SHOCK']\nANOMALY x\n")
    (logs / "spy_intraday_20260708.csv").write_text("minute,close\n0,500.0\n100,502.5\n200,499.0\n")
    return db, logs


def test_record_summarizes_day(tmp_path):
    db, logs = _seed(tmp_path)
    out = logs / "sessions.jsonl"
    row = record_session("2026-07-08", db_path=str(db), log_dir=str(logs), out=str(out))
    assert row["trades"] == 2 and row["closed"] == 1
    assert abs(row["net_pnl"] - 12.5) < 1e-9
    assert row["halts"] == 2 and row["ran"] is True
    assert row["spy_lo"] == 499.0 and row["spy_hi"] == 502.5


def test_record_is_idempotent(tmp_path):
    db, logs = _seed(tmp_path)
    out = logs / "sessions.jsonl"
    record_session("2026-07-08", db_path=str(db), log_dir=str(logs), out=str(out))
    record_session("2026-07-08", db_path=str(db), log_dir=str(logs), out=str(out))
    rows = read_sessions(str(out))
    assert len(rows) == 1 and rows[0]["date"] == "2026-07-08"


def test_read_missing_is_empty(tmp_path):
    assert read_sessions(str(tmp_path / "nope.jsonl")) == []
