"""War-room pure functions + the kill-switch contract with the bot's entry gate."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from src.main import entries_allowed

_spec = importlib.util.spec_from_file_location(
    "warroom", Path(__file__).resolve().parents[1] / "dashboard" / "warroom.py")
warroom = importlib.util.module_from_spec(_spec)
sys.modules["warroom"] = warroom
_spec.loader.exec_module(warroom)


def test_kill_switch_contract(tmp_path):
    """Dashboard writes the flag; the bot's entry gate must read the SAME truth."""
    flag = tmp_path / "entries_disabled.flag"
    assert entries_allowed(flag) is True
    flag.write_text("2026-07-20T12:00:00")           # dashboard pressed KILL
    assert entries_allowed(flag) is False
    flag.unlink()                                    # dashboard pressed RESUME
    assert entries_allowed(flag) is True


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
         "ledger": [], "risk": {}, "trades": [], "actions": [], "kill": False,
         "logger_fresh": False, "heartbeat": False, "test_gate": "", "log_exists": False}
    h = warroom.render(s)
    for probe in ("WAR ROOM", "Account", "Risk", "System health", "Controls",
                  "KILL SWITCH", "FORCE FLATTEN"):
        assert probe in h


def test_render_shows_kill_state():
    s = {"now": "t", "day": "d", "broker": None, "broker_age": 0.0, "vrp": {},
         "ledger": [{"date": "2026-07-03", "net_liq": 1_000_000.0}],
         "risk": {"halted": False, "trades_today": 4}, "trades": [], "actions": [],
         "kill": True, "logger_fresh": False, "heartbeat": True, "test_gate": "PASS x",
         "log_exists": True}
    h = warroom.render(s)
    assert "RESUME ENTRIES" in h and "DISABLED (kill switch)" in h
    assert "Trades left today" in h and ">0<" in h
