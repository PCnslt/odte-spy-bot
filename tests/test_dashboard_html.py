"""Tests for the generated HTML status dashboard (reads trades.db, emits self-contained HTML)."""
from __future__ import annotations

import sqlite3

from src.dashboard_html import generate, render_body, render_page
from src.utils.trade_log import TradeLog


def _seed(path, pnls, widths=None):
    TradeLog(str(path)).close()
    conn = sqlite3.connect(str(path))
    for i, pnl in enumerate(pnls):
        w = (widths[i] if widths else 5.0)
        conn.execute(
            "INSERT INTO trades (opened_at, closed_at, kind, pnl, width, limit_exit, p_bad_fill)"
            " VALUES (?,?,?,?,?,?,?)",
            (f"2026-07-{(i % 27) + 1:02d}T10:00:00", "x", "bull_put", pnl, w, 1, 0.2))
    conn.commit(); conn.close()


def test_empty_renders_armed(tmp_path):
    body = render_body(str(tmp_path / "none.db"))
    assert "ODTE-SPY-BOT" in body
    assert "hasn't traded yet" in body            # plain-English bottom line present
    assert "Pre-registered experiments" in body and "H10" in body
    assert "ARMED · HEALTHY" in body


def test_with_trades_shows_numbers_and_chart(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, [6.0] * 8 + [-4.0] * 2, widths=[5.0] * 5 + [10.0] * 5)
    body = render_body(str(db))
    assert "<svg" in body                          # equity sparkline drawn once trades exist
    assert "$5:5" in body and "$10:5" in body       # real width-experiment counts
    assert "TRADING · HEALTHY" in body


def test_render_page_is_standalone_html(tmp_path):
    page = render_page(str(tmp_path / "none.db"))
    assert page.startswith("<!doctype html>") and page.rstrip().endswith("</body></html>")
    assert "<meta name=\"viewport\"" in page


def test_generate_writes_file(tmp_path):
    out = generate(str(tmp_path / "none.db"), str(tmp_path / "d" / "status.html"))
    assert out.exists() and out.read_text().startswith("<!doctype html>")
