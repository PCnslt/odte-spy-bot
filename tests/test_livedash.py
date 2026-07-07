"""Tests for the live dashboard page + the log-activity tail (no server, no IBKR)."""
from __future__ import annotations

from datetime import date

from src.livedash import build_html
from src.session_chart import tail_activity

SAMPLE_LOG = (
    "2026-07-07 09:25:05 INFO ib_insync.client | Connecting to 127.0.0.1:4002\n"
    "2026-07-07 09:25:05 INFO ib_insync.wrapper | Warning 2104, reqId -1: farm OK\n"
    "09:25 Gateway authenticated — starting the session.\n"
    "2026-07-07 09:25:10,460 INFO alerts | alert: [INFO] Bot starting: CREDIT SPREADS\n"
    "2026-07-07 13:19:15,133 INFO alerts | alert: [WARN] ANOMALY ['PRICE_SHOCK']: halted + flattened\n")


def test_tail_activity_filters_noise_keeps_narrative():
    acts = tail_activity(SAMPLE_LOG)
    joined = " | ".join(m for _, m in acts)
    assert "ib_insync" not in joined and "Warning" not in joined     # noise dropped
    assert "Bot starting: CREDIT SPREADS" in joined                  # alert kept, prefix stripped
    assert "Gateway authenticated" in joined                          # runner echo kept
    assert "ANOMALY" in joined and "halted" in joined
    # times captured
    assert any(t == "13:19:15" for t, _ in acts)


def test_build_html_live_page(tmp_path):
    (tmp_path / "daily_20260707.log").write_text(SAMPLE_LOG)
    spy = [(0, 749.25), (229, 752.4), (390, 751.3)]                  # includes the 13:19 halt x
    html = build_html(db_path=str(tmp_path / "none.db"), log_dir=str(tmp_path),
                      day=date(2026, 7, 7), spy=spy)
    assert 'http-equiv="refresh"' in html                            # auto-refreshes
    assert "LIVE" in html
    assert "<svg" in html and "<circle" in html                      # SPY line + the halt marker
    assert "Live activity log" in html and "Bot starting" in html    # logs panel present
    assert "SPY now" in html and "749" not in html.split("SPY now")[0]  # shows latest price


def test_build_html_waits_gracefully_without_spy(tmp_path):
    html = build_html(db_path=str(tmp_path / "none.db"), log_dir=str(tmp_path),
                      day=date(2026, 7, 7), spy=[])
    assert "Waiting for the first SPY pull" in html                  # no crash, honest message
