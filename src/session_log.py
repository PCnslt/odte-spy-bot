"""Per-day session history — so nothing gets overwritten and you can see performance over time.

The raw data is already kept per day (daily_*.log, spy_intraday_*.csv) and trades.db is
cumulative. This adds a durable one-row-per-day summary (logs/sessions.jsonl) — trades, P&L,
halts, SPY range — that the dashboard renders as a running history + all-time equity curve.
Appended at end of day; idempotent (re-recording a date replaces its row).

    python -m src.session_log --record --date 2026-07-07
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path

SESSIONS = "logs/sessions.jsonl"


def _trade_stats(db_path: str, day: str) -> tuple[int, int, float]:
    """(#opened, #closed, net_pnl) for trades opened on `day`."""
    if not Path(db_path).exists():
        return (0, 0, 0.0)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT pnl, closed_at FROM trades WHERE opened_at LIKE ? "
            "AND IFNULL(exit_reason,'') != 'reconciled_unfilled'",   # not a real trade
            (f"{day}%",)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    closed = sum(1 for _, cl in rows if cl)
    net = sum((p or 0.0) for p, cl in rows if cl)
    return len(rows), closed, round(net, 2)


def _log_stats(log_dir: str, day: str) -> tuple[int, bool]:
    """(#anomaly halts, session-ran?) from the day's log."""
    p = Path(f"{log_dir}/daily_{day.replace('-', '')}.log")
    if not p.exists():
        return (0, False)
    t = p.read_text(errors="ignore")
    return (t.count("ANOMALY"), "Bot starting" in t)


def _spy_range(log_dir: str, day: str) -> tuple[float | None, float | None]:
    """(low, high) SPY close for the day, from the saved intraday CSV."""
    p = Path(f"{log_dir}/spy_intraday_{day.replace('-', '')}.csv")
    if not p.exists():
        return (None, None)
    lo = hi = None
    try:
        for r in csv.DictReader(p.open()):
            raw = r.get("close") or r.get("price") or r.get("spy")
            if raw is None:
                continue
            c = float(raw)
            lo = c if lo is None else min(lo, c)
            hi = c if hi is None else max(hi, c)
    except Exception:
        return (None, None)
    return (lo, hi)


def read_sessions(out: str = SESSIONS) -> list[dict]:
    p = Path(out)
    if not p.exists():
        return []
    rows = []
    for ln in p.read_text().splitlines():
        ln = ln.strip()
        if ln:
            try:
                rows.append(json.loads(ln))
            except Exception:
                pass
    return sorted(rows, key=lambda r: r.get("date", ""))


def record_session(day, db_path: str = "trades.db", log_dir: str = "logs",
                   out: str = SESSIONS) -> dict:
    day = str(day)
    n, closed, net = _trade_stats(db_path, day)
    halts, ran = _log_stats(log_dir, day)
    lo, hi = _spy_range(log_dir, day)
    row = {"date": day, "ran": ran, "trades": n, "closed": closed, "net_pnl": net,
           "halts": halts, "spy_lo": lo, "spy_hi": hi}
    rows = [r for r in read_sessions(out) if r.get("date") != day] + [row]
    rows.sort(key=lambda r: r["date"])
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return row


def main() -> None:
    p = argparse.ArgumentParser(description="Record a day's session summary into history")
    p.add_argument("--record", action="store_true")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--db", default="trades.db")
    p.add_argument("--logs", default="logs")
    a = p.parse_args()
    if a.record:
        print(json.dumps(record_session(a.date, a.db, a.logs)))


if __name__ == "__main__":
    main()
