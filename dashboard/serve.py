"""View-only status server — serves src.dashboard_html's page at http://127.0.0.1:8090.

Owner order 2026-07-20: ONE dashboard module (src/dashboard_html.py, the EOD/status page),
served live, VIEW-ONLY. This wrapper renders that page from FILES ONLY on each request
(ledger, logs, trades.db — never a broker connection, so it can't disturb the Gateway),
cached for 10s. Intraday freshness comes from the files the bot writes as it runs; the
page carries a 15s meta-refresh. There is NO do_POST — nothing here can alter the bot.

    python dashboard/serve.py            # serve http://127.0.0.1:8090
    python dashboard/serve.py --once     # print one rendered page (smoke test)
"""
from __future__ import annotations

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
_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "html": ""}


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
