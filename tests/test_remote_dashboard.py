"""Remote-exposure invariants for the view-only dashboard (Cloudflare tunnel era).

1. No secret may enter the repo: tunnel creds live in ~/.cloudflared/, the Basic-Auth hash
   in ~/.config/odte/dash_auth — both outside the tree, both gitignored defensively.
2. The server must enforce auth whenever the auth file exists, with NO localhost bypass
   (the tunnel hands remote requests to the server AS localhost connections).
3. The tunnel shim must refuse to expose an unauthenticated dashboard.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("dserve2", ROOT / "dashboard" / "serve.py")
dserve = importlib.util.module_from_spec(_spec)
sys.modules["dserve2"] = dserve
_spec.loader.exec_module(dserve)


def _basic(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def test_auth_open_when_no_file(tmp_path):
    assert dserve.auth_ok(None, tmp_path / "absent") is True     # local-only mode unchanged


def test_auth_enforced_when_file_exists(tmp_path):
    f = tmp_path / "dash_auth"
    f.write_text(f"shawn:{hashlib.sha256(b'hunter2').hexdigest()}\n")
    assert dserve.auth_ok(_basic("shawn", "hunter2"), f) is True
    assert dserve.auth_ok(_basic("shawn", "wrong"), f) is False
    assert dserve.auth_ok(_basic("bob", "hunter2"), f) is False
    assert dserve.auth_ok(None, f) is False                      # no header -> denied
    assert dserve.auth_ok("Bearer xyz", f) is False
    assert dserve.auth_ok("Basic not-base64!!", f) is False


def test_no_localhost_bypass_in_server():
    src = (ROOT / "dashboard" / "serve.py").read_text()
    assert "client_address" not in src, "localhost bypass would defeat tunnel auth"
    assert 'do_GET' in src and "auth_ok(self.headers.get" in src


def test_no_secrets_tracked_and_gitignored():
    gi = (ROOT / ".gitignore").read_text()
    for needle in (".cloudflared", "dash_auth", "tunnel.log"):
        assert needle in gi, f".gitignore must cover {needle}"
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True).stdout
    for banned in ("cloudflared/", "dash_auth", ".pem"):
        assert banned not in tracked, f"secret-bearing path tracked: {banned}"


def test_tunnel_shim_refuses_without_auth():
    sh = (ROOT / "dashboard" / "run_tunnel.sh").read_text()
    assert "REFUSING to open a public tunnel" in sh
    assert 'if [ ! -s "$AUTH" ]' in sh


def test_view_only_still_sealed():
    assert not hasattr(dserve.Handler, "do_POST")
