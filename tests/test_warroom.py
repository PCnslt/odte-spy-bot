"""War-room pure functions. VIEW-ONLY contract (owner order 2026-07-20): the dashboard must
not be able to alter the bot — no POST handler, no control endpoints, no flag files."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "warroom", Path(__file__).resolve().parents[1] / "dashboard" / "warroom.py")
warroom = importlib.util.module_from_spec(_spec)
sys.modules["warroom"] = warroom
_spec.loader.exec_module(warroom)


def test_view_only_no_post_no_controls():
    """The owner-ordered invariant: nothing on this page can change the bot's behavior."""
    assert not hasattr(warroom.Handler, "do_POST")
    src = (Path(__file__).resolve().parents[1] / "dashboard" / "warroom.py").read_text()
    for banned in ("/control/", "entries_disabled.flag", "subprocess"):
        assert banned not in src, f"view-only violation: {banned}"


def test_sev_color_logic():
    assert warroom.sev(True) == "ok"
    assert warroom.sev(False) == "crit"
    assert warroom.sev(False, warn=True) == "warn"
    assert warroom.sev(None) == "na"


def test_fmt_money():
    assert warroom.fmt_money(None) == "—"
    assert warroom.fmt_money(999660.25) == "$999,660.25"


def test_read_jsonl_tolerates_garbage(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a":1}\nnot json\n{"b":2}\n')
    assert warroom.read_jsonl(p) == [{"a": 1}, {"b": 2}]
    assert warroom.read_jsonl(tmp_path / "missing.jsonl") == []


def test_render_smoke_with_empty_state():
    """Renders a full page (all panels) from a totally empty/offline state — no crashes."""
    s = {"now": "t", "day": "2026-07-20", "broker": None, "broker_age": 0.0, "vrp": {},
         "ledger": [], "risk": {}, "trades": [], "actions": [],
         "logger_fresh": False, "heartbeat": False, "test_gate": "", "log_exists": False,
         "g2fwd": {"sessions": 0, "snap_days": 0, "basis_fills": 0}}
    h = warroom.render(s)
    for probe in ("WAR ROOM", "Account", "Risk", "System health",
                  "Architecture", "Quote logger (id 49)", "Broker truth",
                  "G2-FWD sessions", "VRP snap days", "Next milestones", "VIEW-ONLY"):
        assert probe in h
    for gone in ("KILL SWITCH", "FORCE FLATTEN", "/control/"):
        assert gone not in h


def test_render_shows_gate_progress():
    s = {"now": "t", "day": "d", "broker": None, "broker_age": 0.0, "vrp": {},
         "ledger": [{"date": "2026-07-03", "net_liq": 1_000_000.0}],
         "risk": {"halted": False, "trades_today": 4}, "trades": [], "actions": [],
         "logger_fresh": True, "heartbeat": True, "test_gate": "PASS x",
         "log_exists": True, "g2fwd": {"sessions": 12, "snap_days": 3, "basis_fills": 7}}
    h = warroom.render(s)
    assert "12 / 60" in h and "7 / 40" in h and "Trades left today" in h and ">0<" in h
