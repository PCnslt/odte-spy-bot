"""Reconciliation tool: book math, conservative dangling-row resolution, NetLiq ledger
baseline selection, and gap flagging in the report."""
from __future__ import annotations

import sqlite3

from datetime import date

from src.reconcile import (BrokerSnap, book_snapshot, build_report, prior_netliq,
                           resolve_dangling, upsert_netliq_ledger, read_netliq_ledger)
from src.utils.trade_log import TradeLog


def _db(tmp_path):
    p = tmp_path / "trades.db"
    tl = TradeLog(p)                                     # creates schema
    # two closed trades on 2026-07-08
    i1 = tl.open_trade(opened_at="2026-07-08T10:52:00", kind="bull_put", quantity=5,
                       credit_est=0.84)
    tl.close_trade(i1, closed_at="2026-07-08T12:40:48", exit_reason="take_profit",
                   exit_cost_est=0.45, exit_cost_fill=None, credit_fill=0.90, pnl=257.0,
                   limit_exit=False)
    i2 = tl.open_trade(opened_at="2026-07-08T12:43:20", kind="bear_call", quantity=5,
                       credit_est=0.62)
    tl.close_trade(i2, closed_at="2026-07-08T12:55:35", exit_reason="flatten",
                   exit_cost_est=0.58, exit_cost_fill=None, credit_fill=0.72, pnl=-128.0,
                   limit_exit=False)
    # one dangling unfilled entry (the 2026-07-08 id=2 case): opened, never closed, no fill
    tl.open_trade(opened_at="2026-07-08T10:31:09", kind="bull_put", quantity=5,
                  credit_est=1.06)
    tl.close()
    return str(p)


def test_book_snapshot_counts_and_pnl(tmp_path):
    snap = book_snapshot(_db(tmp_path), __import__("datetime").date(2026, 7, 8))
    assert snap.n_closed == 2
    assert snap.net_pnl == 129.0                          # 257 - 128
    assert len(snap.dangling) == 1
    assert snap.dangling[0]["credit_fill"] is None


def test_resolve_only_touches_never_filled(tmp_path):
    db = _db(tmp_path)
    # add a FILLED-but-still-open row — resolve must NOT touch it (needs human eyes)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO trades (opened_at, kind, quantity, credit_est, credit_fill) "
                 "VALUES ('2026-07-08T14:00:00','bear_call',5,0.5,0.48)")
    conn.commit()
    conn.close()

    resolved = resolve_dangling(db, "2026-07-09T09:45:00")
    assert len(resolved) == 1                             # only the unfilled one

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    fixed = conn.execute("SELECT * FROM trades WHERE id=?", (resolved[0],)).fetchone()
    assert fixed["closed_at"] == "2026-07-09T09:45:00"
    assert fixed["pnl"] == 0 and fixed["exit_reason"] == "reconciled_unfilled"
    still_open = conn.execute(
        "SELECT COUNT(*) c FROM trades WHERE closed_at IS NULL").fetchone()["c"]
    conn.close()
    assert still_open == 1                                # the filled-but-open row untouched


def test_prior_netliq_is_day_over_day():
    ledger = [
        {"date": "2026-07-07", "ts": "2026-07-07T16:00:00", "net_liq": 1000086.0},
        {"date": "2026-07-08", "ts": "2026-07-08T16:00:00", "net_liq": 999928.0},
    ]
    # reconciling 07-08 must anchor on 07-07's close, NOT the same-day 07-08 snapshot
    assert prior_netliq(ledger, date(2026, 7, 8))["net_liq"] == 1000086.0
    assert prior_netliq(ledger, date(2026, 7, 7)) is None          # nothing earlier
    assert prior_netliq(ledger, date(2026, 7, 9))["net_liq"] == 999928.0


def test_ledger_upsert_is_one_row_per_day(tmp_path):
    path = str(tmp_path / "netliq.jsonl")
    upsert_netliq_ledger(path, {"date": "2026-07-08", "ts": "2026-07-08T16:00:00",
                                "net_liq": 999928.0})
    upsert_netliq_ledger(path, {"date": "2026-07-08", "ts": "2026-07-08T18:41:00",
                                "net_liq": 999928.0})   # same day again -> replace, not append
    upsert_netliq_ledger(path, {"date": "2026-07-07", "ts": "2026-07-07T16:00:00",
                                "net_liq": 1000086.0})
    rows = read_netliq_ledger(path)
    assert len(rows) == 2                                # one per day
    assert [r["date"] for r in rows] == ["2026-07-07", "2026-07-08"]   # date-sorted
    assert rows[1]["ts"] == "2026-07-08T18:41:00"        # kept the latest same-day write


def test_report_flags_gap(tmp_path):
    book = book_snapshot(_db(tmp_path), __import__("datetime").date(2026, 7, 8))
    broker = BrokerSnap(True, ts="2026-07-08T16:05:00", account="DUR193467",
                        net_liq=999928.0, realized_pnl=-158.0)
    baseline = {"date": "2026-07-07", "net_liq": 1000086.0}
    rep = build_report(book, broker, baseline)
    assert "investigate" in rep                           # book +129 vs actual -158 => big gap
    assert "$+129.00" in rep


def test_report_book_only_when_broker_down(tmp_path):
    book = book_snapshot(_db(tmp_path), __import__("datetime").date(2026, 7, 8))
    rep = build_report(book, BrokerSnap(False, note="Gateway down"), None)
    assert "UNAVAILABLE" in rep and "Gateway down" in rep


def test_past_date_reconcile_does_not_overwrite_ledger(tmp_path, monkeypatch):
    """A --date <pastday> run reports but must NOT record live NetLiq as that day's close —
    otherwise a 9:50am run would clobber yesterday's settled figure with a balance that already
    contains today's trades."""
    from datetime import datetime
    import src.reconcile as R
    db = _db(tmp_path)
    ledger = str(tmp_path / "netliq.jsonl")
    R.upsert_netliq_ledger(ledger, {"date": "2026-07-07", "ts": "2026-07-07T16:00:00",
                                    "net_liq": 1_000_086.0})
    R.upsert_netliq_ledger(ledger, {"date": "2026-07-08", "ts": "2026-07-09T03:20:00",
                                    "net_liq": 1_000_014.40})   # settled close
    monkeypatch.setattr(R, "broker_snapshot",
                        lambda *a, **k: BrokerSnap(True, ts="x", net_liq=999_500.0))

    # reconcile the PAST day at 9:50am today — must leave the 2026-07-08 entry untouched
    R.reconcile(date(2026, 7, 8), db_path=db, ledger_path=ledger,
                now=datetime(2026, 7, 9, 9, 50))
    jul8 = [r for r in read_netliq_ledger(ledger) if r["date"] == "2026-07-08"][0]
    assert jul8["net_liq"] == 1_000_014.40                      # NOT the 999,500 live pull

    # reconcile the CURRENT day — that one SHOULD record
    R.reconcile(date(2026, 7, 9), db_path=db, ledger_path=ledger,
                now=datetime(2026, 7, 9, 16, 0))
    assert any(r["date"] == "2026-07-09" and r["net_liq"] == 999_500.0
               for r in read_netliq_ledger(ledger))
