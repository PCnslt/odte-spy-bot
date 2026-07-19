"""View-only status server — serves src.dashboard_html's page at http://127.0.0.1:8090.

Owner order 2026-07-20: ONE dashboard module (src/dashboard_html.py, the EOD/status page),
served live, VIEW-ONLY. This wrapper renders that page from FILES ONLY on each request
(ledger, logs, trades.db — never a broker connection, so it can't disturb the Gateway),
cached for 10s. Intraday freshness comes from the files the bot writes as it runs; the
page carries a 15s meta-refresh. There is NO do_POST — nothing here can alter the bot.

Optional Basic Auth for remote exposure (dashboard/setup_remote.sh): if the credentials
file ~/.config/odte/dash_auth exists (format "user:sha256hex(password)" — OUTSIDE the repo,
created by the OWNER; this code never sees or stores a plaintext password), every request
must authenticate. Deliberately NO localhost bypass: a Cloudflare tunnel hands remote
requests to this server AS localhost connections, so a localhost skip would defeat the
auth entirely. No file -> no prompt (local-only behavior unchanged).

    python dashboard/serve.py            # serve http://127.0.0.1:8090
    python dashboard/serve.py --once     # print one rendered page (smoke test)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.dashboard_html import render_page  # noqa: E402

PORT = 8090
CACHE_S = 10.0
AUTH_FILE = Path.home() / ".config" / "odte" / "dash_auth"   # owner-created, never in git
_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "html": ""}


def auth_ok(header: str | None, auth_file: Path = AUTH_FILE) -> bool:
    """True when no credentials file exists (local-only mode) or the Basic header matches
    it. Constant-time compare of "user:sha256hex"; malformed anything -> False."""
    if not auth_file.exists():
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, pw = base64.b64decode(header[6:]).decode().split(":", 1)
        want = auth_file.read_text().strip()
        got = f"{user}:{hashlib.sha256(pw.encode()).hexdigest()}"
        return hmac.compare_digest(want.encode(), got.encode())
    except Exception:
        return False


def page() -> str:
    with _lock:
        if time.time() - _cache["ts"] > CACHE_S:
            try:
                _cache["html"] = render_page(str(REPO / "trades.db"))
            except Exception as exc:               # fail-soft: an error page, never a crash
                _cache["html"] = f"<h1>dashboard render error</h1><pre>{exc}</pre>"
            _cache["ts"] = time.time()
        return _cache["html"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):                     # quiet
        pass

    def do_GET(self):
        if not auth_ok(self.headers.get("Authorization")):
            body = b"authentication required"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="ODTE status"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        data = page().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # NO do_POST — view-only by owner order (2026-07-20). Bot control is terminal-only.


def main() -> None:                                # pragma: no cover
    import os
    os.chdir(REPO)                                 # relative paths (logs/, trades.db) resolve
    if "--once" in sys.argv:
        print(page()[:2000])
        return
    print(f"status dashboard: http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":                         # pragma: no cover
    main()
