"""Live-status additions to the ONE dashboard (src/dashboard_html.py) + the view-only server.

Owner order 2026-07-20: single dashboard module, served view-only at :8090. These tests pin
the outcome classifier, the 2FA proxy, the G2-FWD earliest-date math, and the no-controls
contract of dashboard/serve.py.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

from src.dashboard_html import (add_weekdays, earliest_g2fwd, last_auth_date,
                                recent_outcomes, render_page, session_outcome)

ROOT = Path(__file__).resolve().parents[1]


def test_session_outcome_classification():
    assert session_outcome("Weekend; exiting.") == "weekend"
    assert session_outcome(
        "15:30 Past 1530 ET with no authenticated Gateway (weekly 2FA not done?) "
        "— nothing to trade today. Clean exit.") == "missed_2fa"
    assert session_outcome("ERROR: tests FAILED on pulled code") == "missed_testgate"
    assert session_outcome(
        "Gateway authenticated.\nStarting the session.\nGAP GUARD: gap") == "gap_blocked"
    assert session_outcome(
        "Gateway authenticated.\nStarting the session.\nOPEN bull_put") == "ran"


def test_recent_outcomes_and_last_auth(tmp_path):
    (tmp_path / "daily_20260713.log").write_text("no authenticated Gateway — nothing to trade")
    (tmp_path / "daily_20260714.log").write_text("Gateway authenticated.\nStarting the session.")
    (tmp_path / "daily_20260718.log").write_text("Weekend; exiting.")
    assert recent_outcomes(tmp_path) == [("20260713", "missed_2fa"), ("20260714", "ran")]
    assert last_auth_date(tmp_path) == "20260714"
    assert last_auth_date(tmp_path / "nope") is None


def test_add_weekdays_skips_weekends():
    assert add_weekdays(date(2026, 7, 20), 5) == date(2026, 7, 24)    # Mon +5 -> Fri
    assert add_weekdays(date(2026, 7, 24), 2) == date(2026, 7, 27)    # Fri +2 -> Mon


def test_earliest_g2fwd_binding_is_structure_trades():
    # zero evidence today: 60 sessions ~12 weeks; 200 trades @4/day from 08-10 = 50 sessions
    est, binding = earliest_g2fwd({"sessions": 0, "basis_fills": 0}, date(2026, 7, 20),
                                  structure_start=date(2026, 8, 10))
    assert binding == "200 structure trades"
    assert est == add_weekdays(date(2026, 8, 10), 50)
    # nearly-complete evidence: sessions become binding
    est2, binding2 = earliest_g2fwd({"sessions": 5, "basis_fills": 39}, date(2026, 7, 20),
                                    structure_start=date(2026, 7, 20))
    assert binding2 == "sessions"


def test_render_page_includes_live_status_and_refresh():
    h = render_page(str(ROOT / "definitely_missing.db"))
    for probe in ("Architecture — live module map", "2FA / Gateway auth", "SUNDAY EVENING",
                  "Sessions missed", "G2-FWD progress", "G2-FWD earliest verdict",
                  'http-equiv="refresh"', "view-only"):
        assert probe in h
    for gone in ("KILL SWITCH", "FORCE FLATTEN", "/control/"):
        assert gone not in h


def test_serve_is_view_only():
    spec = importlib.util.spec_from_file_location("dserve", ROOT / "dashboard" / "serve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dserve"] = mod
    spec.loader.exec_module(mod)
    assert not hasattr(mod.Handler, "do_POST")
    src = (ROOT / "dashboard" / "serve.py").read_text()
    for banned in ("/control/", "entries_disabled", "subprocess"):
        assert banned not in src
