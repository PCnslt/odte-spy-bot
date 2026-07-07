"""Tests for the intraday SPY session chart (pure SVG + log/CSV parsing; no network)."""
from __future__ import annotations

from datetime import date

from src.session_chart import (build_session_svg, load_spy, parse_log_events, render_svg)


def test_render_svg_draws_line_markers_and_tooltips():
    spy = [(0, 700.0), (100, 701.0), (200, 699.5), (390, 702.0)]
    events = [(120, "halt", "HALT · Price Shock · 11:30"), (200, "open", "OPEN bull_put · 12:50")]
    svg = render_svg(spy, events)
    assert svg.startswith("<svg") and "polyline" in svg
    assert svg.count("<circle") == 2                 # one dot per event
    assert "Price Shock" in svg                      # tooltip text preserved
    assert "1 halt(s)" in svg and "1 trade(s)" in svg  # legend counts
    assert render_svg([], []) == ""                  # no SPY -> no chart


def test_parse_log_events_halts_and_gap():
    log = (
        "2026-07-06 13:19:15,133 INFO alerts | alert: [WARN] ANOMALY ['PRICE_SHOCK']: halted\n"
        "2026-07-06 13:21:16,946 INFO alerts | alert: [WARN] ANOMALY ['PRICE_SHOCK', 'IV_SPIKE']: halted\n"
        "2026-07-06 10:05:00,000 INFO alerts | alert: [WARN] GAP GUARD: overnight gap +1.2%\n"
        "2026-07-06 11:00:00,000 INFO main | nothing to see here\n")
    ev = parse_log_events(log)
    assert len(ev) == 3
    halts = [e for e in ev if e[1] == "halt"]
    assert len(halts) == 2
    assert halts[0][0] == (13 - 9) * 60 + 19 - 30     # 13:19 -> minute 229
    assert "Price Shock" in halts[0][2]
    assert "Iv Spike" in halts[1][2]                  # both kinds in the label
    assert any(t == "gap" for _, t, _ in ev)


def test_load_spy_from_csv(tmp_path):
    csv = tmp_path / "spy.csv"
    csv.write_text(
        ",open,high,low,close,volume\n"
        "2026-07-06 13:30:00+00:00,749.0,749.3,748.9,749.25,1000\n"   # 13:30 UTC = 09:30 ET
        "2026-07-06 13:31:00+00:00,749.25,749.5,749.1,749.40,1200\n")
    pts = load_spy(str(csv))
    assert pts == [(0.0, 749.25), (1.0, 749.40)]
    assert load_spy(str(tmp_path / "missing.csv")) == []


def test_build_session_svg_end_to_end(tmp_path):
    day = date(2026, 7, 6)
    (tmp_path / f"spy_intraday_{day:%Y%m%d}.csv").write_text(
        ",open,high,low,close,volume\n"
        "2026-07-06 13:30:00+00:00,749.0,749.3,748.9,749.25,1000\n"
        "2026-07-06 17:19:00+00:00,751.0,752.4,750.9,751.30,1500\n")   # 13:19 ET
    (tmp_path / f"daily_{day:%Y%m%d}.log").write_text(
        "2026-07-06 13:19:15,133 INFO alerts | alert: [WARN] ANOMALY ['PRICE_SHOCK']: halted\n")
    svg = build_session_svg(day=day, log_dir=str(tmp_path), db_path=str(tmp_path / "none.db"))
    assert svg.startswith("<svg") and "<circle" in svg  # SPY line + the halt marker
    # No CSV -> graceful empty.
    assert build_session_svg(day=date(2020, 1, 1), log_dir=str(tmp_path)) == ""
