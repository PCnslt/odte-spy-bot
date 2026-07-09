"""Tests for the generated HTML status dashboard — clean, account-truth P&L, no editorializing.

chdir into tmp_path so the dashboard's relative logs/ reads (ledger, SPY csvs, session log) are
isolated from the repo's real runtime data."""
from __future__ import annotations

import json
import sqlite3

from src.dashboard_html import generate, render_body, render_page
from src.utils.trade_log import TradeLog

_BANNED = ("Account truth", "book over-states", "GAP", "see reconciliation", "phantom",
           "BOT-RECORDED", "overclaim")


def _seed_trades(path, specs):
    """specs: list of (kind, pnl) closed trades on 2026-07-08."""
    TradeLog(str(path)).close()
    conn = sqlite3.connect(str(path))
    for i, (kind, pnl) in enumerate(specs):
        conn.execute(
            "INSERT INTO trades (opened_at, closed_at, kind, short_strike, long_strike, width,"
            " credit_fill, exit_reason, pnl) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"2026-07-08T1{i}:00:00", "2026-07-08T15:00:00", kind, 740 - i, 730 - i, 10,
             0.9, "take_profit", pnl))
    conn.commit()
    conn.close()


def _seed_ledger(logs_dir, entries):
    logs_dir.mkdir(exist_ok=True)
    with open(logs_dir / "netliq.jsonl", "w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def test_empty_renders_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    body = render_body(str(tmp_path / "none.db"))
    assert "ODTE-SPY-BOT" in body
    assert "Account value" in body and "Total P&amp;L" in body and "Healthy" in body
    assert "Trade log" not in body                         # nothing traded yet
    for bad in _BANNED:
        assert bad not in body


def test_pnl_comes_from_account_ledger_not_book(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Book sums to +$300, but the real account FELL $71.60. The headline must be the account
    # number, never the book — this is the whole point of the rebuild.
    _seed_trades(tmp_path / "t.db",
                 [("bull_put", 257.0), ("bear_call", -128.0), ("bull_put", 171.0)])
    _seed_ledger(tmp_path / "logs", [
        {"date": "2026-07-07", "ts": "2026-07-07T16:00:00", "net_liq": 1_000_086.0},
        {"date": "2026-07-08", "ts": "2026-07-08T16:00:00", "net_liq": 1_000_014.40,
         "orphans": 0},
    ])
    body = render_body(str(tmp_path / "t.db"))
    assert "$1,000,014.40" in body                         # account value tile (cents)
    assert "$-71.60" in body                               # Total P&L = ledger delta, to cents
    assert "$300" not in body and "$+300" not in body      # book cumulative never shown
    assert "Trade log" in body and "Bull put" in body      # trades still listed as detail
    assert "<svg" in body                                  # account-value curve drawn
    for bad in _BANNED:
        assert bad not in body


def test_render_page_is_standalone_html(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    page = render_page(str(tmp_path / "none.db"))
    assert page.startswith("<!doctype html>") and page.rstrip().endswith("</body></html>")
    assert "<meta name=\"viewport\"" in page


def test_generate_writes_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = generate(str(tmp_path / "none.db"), str(tmp_path / "d" / "status.html"))
    assert out.exists() and out.read_text().startswith("<!doctype html>")
