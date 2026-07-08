"""Reconciliation: does the bot's book match the broker's reality?

The 2026-07-08 incident (BUG #1) showed why this has to exist: the bot recorded +$156 for
the day while the paper account actually fell ~$158 — a ~$314 gap caused by a phantom short
(an unfilled entry the pre-fix code market-sold, whose legs then expired). trades.db is the
bot's *belief*; the IBKR account is *truth*. This tool puts them side by side.

What it checks for a given date:
  * BOOK    — trades.db: closed trades + recorded net P&L, and any dangling OPEN rows
              (opened but never closed — usually an entry that never filled).
  * BROKER  — IBKR paper account: NetLiquidation, RealizedPnL, open SPY option positions
              (0DTE should expire flat — anything left is an unmanaged orphan), fill count.
  * TRUTH   — a local NetLiq ledger (logs/netliq.jsonl). Each run appends the account's
              NetLiq, so day-over-day NetLiq *delta* is the real P&L to compare the book to.

It never trades and never moves money — it only reads the account and (with --resolve) tidies
the book by marking never-filled entries as cancelled. Fail-soft: if the Gateway is down it
reports the book alone and says the broker side is unavailable, rather than erroring out.

    python -m src.reconcile                       # reconcile today, print report
    python -m src.reconcile --date 2026-07-08      # a specific date
    python -m src.reconcile --resolve              # also mark dangling unfilled entries closed
    python -m src.reconcile --json                 # machine-readable
    python -m src.reconcile --report-file PATH      # also write the text report to PATH
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

GAP_ALERT = 25.0  # |book - netliq_delta| above this (USD) is flagged for a human


# --- book side (trades.db) -----------------------------------------------------------------
@dataclass
class BookSnap:
    day: str
    n_closed: int
    net_pnl: float
    closed: list[dict]
    dangling: list[dict]          # opened but closed_at IS NULL (usually unfilled entries)


def book_snapshot(db_path: str, day: date) -> BookSnap:
    like = f"{day.isoformat()}%"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        closed = [dict(r) for r in conn.execute(
            "SELECT id, kind, opened_at, closed_at, credit_fill, exit_cost_fill, "
            "exit_reason, pnl, quantity FROM trades "
            "WHERE closed_at LIKE ? ORDER BY closed_at", (like,))]
        # Dangling rows are matched by OPEN date and reported regardless of how old — an
        # unfilled entry from any prior session is still clutter that should be resolved.
        dangling = [dict(r) for r in conn.execute(
            "SELECT id, kind, opened_at, credit_est, credit_fill, quantity FROM trades "
            "WHERE closed_at IS NULL ORDER BY opened_at")]
    finally:
        conn.close()
    net = round(sum((r["pnl"] or 0.0) for r in closed), 2)
    return BookSnap(day.isoformat(), len(closed), net, closed, dangling)


def resolve_dangling(db_path: str, now_iso: str) -> list[int]:
    """Mark never-filled entries (dangling open rows with no credit_fill) as cancelled.
    Returns the ids touched. Deliberately conservative: only rows that clearly never filled
    (credit_fill IS NULL AND pnl IS NULL) are closed, with pnl=0 and an explicit reason — we
    never fabricate a P&L. Rows that carry a pnl are left alone for a human to inspect."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM trades WHERE closed_at IS NULL "
            "AND credit_fill IS NULL AND pnl IS NULL")]
        for tid in ids:
            conn.execute(
                "UPDATE trades SET closed_at=?, exit_reason=?, pnl=0 WHERE id=?",
                (now_iso, "reconciled_unfilled", tid))
        conn.commit()
    finally:
        conn.close()
    return ids


# --- broker side (IBKR, read-only) ---------------------------------------------------------
@dataclass
class BrokerSnap:
    available: bool
    ts: str = ""
    account: str = ""
    net_liq: Optional[float] = None
    realized_pnl: Optional[float] = None      # broker's RealizedPnL tag (SAME-day only)
    unrealized_pnl: Optional[float] = None
    cash: Optional[float] = None
    orphans: list[dict] = field(default_factory=list)   # non-flat SPY option legs
    n_fills: int = 0
    note: str = ""


def broker_snapshot(host: str, port: int, symbol: str, client_id: int = 47,
                    now: Optional[datetime] = None) -> BrokerSnap:
    """Read-only pull of account truth. Fail-soft: returns available=False if the Gateway
    isn't up or the connection times out — the caller still reports the book."""
    now = now or datetime.now()
    try:
        from ib_insync import IB
    except Exception as exc:  # pragma: no cover - ib_insync always present in runtime venv
        return BrokerSnap(False, note=f"ib_insync unavailable: {exc}")
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=15)
    except Exception as exc:
        return BrokerSnap(False, ts=now.isoformat(timespec="seconds"),
                          note=f"IBKR Gateway not reachable on {host}:{port} ({exc}); "
                               "broker truth unavailable — book-only report.")
    try:
        summary = {r.tag: r.value for r in ib.accountSummary()}
        accounts = ib.managedAccounts()
        orphans = []
        for p in ib.positions():
            c = p.contract
            if getattr(c, "secType", "") == "OPT" and c.symbol == symbol and p.position:
                orphans.append({"localSymbol": c.localSymbol, "right": c.right,
                                "strike": float(c.strike), "position": int(p.position),
                                "avgCost": round(float(p.avgCost), 2)})
        try:
            n_fills = len(ib.fills())
        except Exception:
            n_fills = 0

        def _f(tag):
            try:
                return round(float(summary[tag]), 2)
            except (KeyError, ValueError, TypeError):
                return None

        return BrokerSnap(
            True, ts=now.isoformat(timespec="seconds"),
            account=accounts[0] if accounts else "",
            net_liq=_f("NetLiquidation"), realized_pnl=_f("RealizedPnL"),
            unrealized_pnl=_f("UnrealizedPnL"), cash=_f("TotalCashValue"),
            orphans=orphans, n_fills=n_fills)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


# --- NetLiq ledger: the day-over-day P&L ground truth --------------------------------------
def read_netliq_ledger(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text().splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return out


def upsert_netliq_ledger(path: str, entry: dict) -> None:
    """One row per day: replace any existing entry for entry['date'] with this one, so
    re-running on the same day overwrites rather than piling up (which would poison the
    day-over-day baseline and bloat the dashboard equity curve). Rows are kept date-sorted."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    rows = [e for e in read_netliq_ledger(path) if e.get("date") != entry.get("date")]
    rows.append(entry)
    rows.sort(key=lambda e: (e.get("date", ""), e.get("ts", "")))
    with open(path, "w") as fh:
        for e in rows:
            fh.write(json.dumps(e) + "\n")


def prior_netliq(ledger: list[dict], day: date) -> Optional[dict]:
    """Baseline for a day-over-day P&L delta: the most recent ledger entry with a net_liq
    whose DATE is strictly before `day`. Same-day entries are ignored, so re-running today
    still measures against yesterday's close, not this morning's snapshot."""
    d = day.isoformat()
    cand = [e for e in ledger if e.get("net_liq") is not None and e.get("date", "") < d]
    return max(cand, key=lambda e: (e.get("date", ""), e.get("ts", ""))) if cand else None


# --- assembly / reporting ------------------------------------------------------------------
def build_report(book: BookSnap, broker: BrokerSnap, baseline: Optional[dict]) -> str:
    L = [f"=== RECONCILIATION {book.day} ===", ""]
    # BOOK
    L.append("BOOK (trades.db)")
    L.append(f"  closed trades: {book.n_closed:>3}   recorded net P&L: ${book.net_pnl:+,.2f}")
    for r in book.closed:
        ct = (r.get("closed_at") or "")[11:16]
        L.append(f"    id={r['id']} {r['kind']:9s} close {ct} "
                 f"{r.get('exit_reason') or '?':16s} ${(r['pnl'] or 0.0):+,.2f}")
    if book.dangling:
        L.append(f"  dangling OPEN rows (opened, never closed): {len(book.dangling)}")
        for r in book.dangling:
            filled = "unfilled" if r.get("credit_fill") is None else "FILLED-no-close!"
            L.append(f"    id={r['id']} {r['kind']:9s} opened {(r['opened_at'] or '')[11:16]} "
                     f"credit_fill={r.get('credit_fill')}  [{filled}]")
    L.append("")
    # BROKER
    if not broker.available:
        L.append("BROKER (IBKR)  — UNAVAILABLE")
        L.append(f"  {broker.note}")
    else:
        L.append(f"BROKER (IBKR paper {broker.account}) @ {broker.ts}")
        L.append(f"  NetLiquidation ..... ${broker.net_liq:,.2f}" if broker.net_liq is not None
                 else "  NetLiquidation ..... n/a")
        if broker.realized_pnl is not None:
            L.append(f"  RealizedPnL (today) ${broker.realized_pnl:+,.2f}  "
                     "(broker's SAME-day figure)")
        if broker.unrealized_pnl is not None:
            L.append(f"  UnrealizedPnL ...... ${broker.unrealized_pnl:+,.2f}")
        if broker.orphans:
            L.append(f"  Open {broker.orphans[0].get('localSymbol','')[:3] or 'SPY'} option "
                     f"legs: {len(broker.orphans)}  ⚠ NOT FLAT — unmanaged risk:")
            for o in broker.orphans:
                L.append(f"    {o['localSymbol']} {o['right']} {o['strike']:.0f} "
                         f"x{o['position']:+d}")
        else:
            L.append("  Open SPY option legs: 0   [OK — flat]")
        L.append(f"  Fills on record: {broker.n_fills}")
    L.append("")
    # TRUTH: day-over-day NetLiq delta vs book
    L.append("RECONCILIATION")
    L.append(f"  book net P&L ............ ${book.net_pnl:+,.2f}")
    if broker.available and broker.net_liq is not None and baseline is not None:
        delta = round(broker.net_liq - baseline["net_liq"], 2)
        gap = round(book.net_pnl - delta, 2)
        L.append(f"  NetLiq {baseline.get('date','?')} → now: ${baseline['net_liq']:,.2f} → "
                 f"${broker.net_liq:,.2f}  =  ${delta:+,.2f}  (ACTUAL)")
        L.append(f"  gap (book − actual) .... ${gap:+,.2f}"
                 + ("   ⚠  investigate" if abs(gap) > GAP_ALERT else "   ✓ within noise"))
    elif broker.available and broker.net_liq is not None:
        L.append(f"  NetLiq now: ${broker.net_liq:,.2f}  (no prior baseline yet — recorded "
                 "as today's anchor; day-over-day truth starts next run)")
    else:
        L.append("  actual NetLiq delta .... unavailable (Gateway down)")
    return "\n".join(L)


def reconcile(day: Optional[date] = None, db_path: str = "trades.db",
              ledger_path: str = "logs/netliq.jsonl", host: str = "127.0.0.1",
              port: int = 4002, symbol: str = "SPY", resolve: bool = False,
              now: Optional[datetime] = None) -> dict:
    now = now or datetime.now()
    day = day or now.date()
    book = book_snapshot(db_path, day)
    broker = broker_snapshot(host, port, symbol, now=now)

    ledger = read_netliq_ledger(ledger_path)
    baseline = prior_netliq(ledger, day)   # yesterday's close, never a same-day snapshot
    if broker.available and broker.net_liq is not None:
        upsert_netliq_ledger(ledger_path, {
            "date": day.isoformat(), "ts": now.isoformat(timespec="seconds"),
            "net_liq": broker.net_liq, "realized_pnl": broker.realized_pnl,
            "book_net": book.net_pnl, "n_closed": book.n_closed,
            "orphans": len(broker.orphans), "source": "live"})

    resolved: list[int] = []
    if resolve:
        resolved = resolve_dangling(db_path, now.isoformat(timespec="seconds"))
        if resolved:
            book = book_snapshot(db_path, day)  # refresh so the report shows them cleared

    report = build_report(book, broker, baseline)
    if resolved:
        report += f"\n\nRESOLVED: marked {len(resolved)} unfilled entry row(s) cancelled: " \
                  f"{', '.join('id=' + str(i) for i in resolved)}"
    return {"date": day.isoformat(), "book": asdict(book), "broker": asdict(broker),
            "baseline": baseline, "resolved": resolved, "report": report}


def main() -> None:
    ap = argparse.ArgumentParser(description="Reconcile trades.db against the IBKR account")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--db", default="trades.db")
    ap.add_argument("--ledger", default="logs/netliq.jsonl")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--resolve", action="store_true",
                    help="mark dangling never-filled entry rows as cancelled")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--report-file", help="also write the text report here")
    a = ap.parse_args()
    day = date.fromisoformat(a.date) if a.date else None
    result = reconcile(day, db_path=a.db, ledger_path=a.ledger, host=a.host, port=a.port,
                       symbol=a.symbol, resolve=a.resolve)
    if a.report_file:
        Path(a.report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(a.report_file).write_text(result["report"] + "\n")
    print(json.dumps(result, default=str, indent=2) if a.json else result["report"])


if __name__ == "__main__":
    main()
