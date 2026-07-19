"""Remote-access (Tailscale) wiring + the 'never expose to the public internet' invariant.

The war room has live trade controls (KILL / FORCE FLATTEN). Remote access is allowed ONLY
over a private tailnet (`tailscale serve`). Exposing it publicly (`tailscale funnel`, or a
launchd agent that binds off-localhost) is a safety violation these tests fail the build on.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_warroom_launchd_plist_valid_and_always_on():
    with (ROOT / "deploy" / "com.pcnslt.warroom.plist").open("rb") as fh:
        d = plistlib.load(fh)
    assert d["Label"] == "com.pcnslt.warroom"
    assert d["KeepAlive"] is True and d["RunAtLoad"] is True
    args = d["ProgramArguments"]
    assert args[0].endswith("venv/bin/python")          # the deps-complete interpreter
    assert args[1].endswith("dashboard/warroom.py")


def test_setup_tunnel_serves_privately_never_funnels():
    lines = (ROOT / "dashboard" / "setup_tunnel.sh").read_text().splitlines()
    assert any("serve" in ln for ln in lines), "setup must expose via `tailscale serve`"
    # `funnel` (public internet) may appear ONLY in a warning comment/echo, never as a command.
    for ln in lines:
        if "funnel" in ln:
            s = ln.strip()
            assert s.startswith("#") or "echo" in ln or "NEVER" in ln, f"funnel invoked: {ln}"


def test_warroom_binds_localhost_only():
    """No inbound port on the Mac: the server must bind 127.0.0.1, not 0.0.0.0. Tailscale
    reaches it locally; nothing else can."""
    src = (ROOT / "dashboard" / "warroom.py").read_text()
    assert '("127.0.0.1"' in src.replace(" ", "")  # ThreadingHTTPServer(("127.0.0.1", PORT)...
    assert '0.0.0.0' not in src


def test_runner_does_not_also_start_warroom():
    """The always-on launchd agent owns :8090; the session runner must not spawn a rival copy."""
    runner = (ROOT / "scripts" / "run_paper_day.sh").read_text()
    for ln in runner.splitlines():
        if ln.strip().startswith("#"):
            continue
        assert "warroom.py" not in ln, f"runner starts a second war room: {ln}"
