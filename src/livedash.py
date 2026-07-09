"""Live local dashboard — an auto-refreshing view of the RUNNING session.

Unlike the once-a-day published snapshot, this ticks: it re-pulls SPY from IBKR, re-reads
trades.db and today's log, and redraws every few seconds so you can watch the bot think —
the SPY line building, halts/trades landing on it, and the live activity log scrolling.

Standalone and READ-ONLY: a separate process from the trading bot (its own IBKR client id),
so it cannot affect trading. Serves on 127.0.0.1 only (local viewing).

    python -m src.livedash            # then open http://127.0.0.1:8080  (on the Mac)

Design note: ib_insync must run on the MAIN thread, so the SPY refresh loop lives there and
the HTTP server runs in a daemon thread reading a shared snapshot.
"""
from __future__ import annotations

import argparse
import http.server
import threading
import time
from datetime import datetime
from pathlib import Path

from .briefing import briefing
from .reconcile import prior_netliq, read_netliq_ledger
from .session_chart import parse_log_events, render_svg, tail_activity, trade_events

REFRESH_S = 12
_STATE: dict = {"spy": [], "ts": None, "netliq": None}

_CSS = """
:root{--ink:#0e141b;--panel:#151d27;--line:#27323f;--text:#d8dee7;--muted:#8894a3;--faint:#5b6773;
--accent:#45c4b8;--good:#49b65f;--warn:#d9a430;--crit:#e0533c;--mono:ui-monospace,"SF Mono",Menlo,monospace;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--ink);color:var(--text);font-family:var(--sans)}
.wrap{max-width:960px;margin:0 auto;padding:18px 18px 40px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;
padding-bottom:12px;border-bottom:1px solid var(--line)}
.top b{font-family:var(--mono);font-size:15px}.live{font-family:var(--mono);font-size:11px;color:var(--crit)}
.live .d{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--crit);margin-right:5px;
animation:bl 1.4s steps(2) infinite}@keyframes bl{50%{opacity:.2}}
@media(prefers-reduced-motion:reduce){.live .d{animation:none}}
.bl{display:flex;gap:11px;align-items:flex-start;margin-top:14px;padding:12px 14px;border:1px solid var(--line);border-radius:10px}
.bl .b{font-size:14px}.bl .b b{color:var(--text)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card .l{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint)}
.card .v{font-family:var(--mono);font-size:23px;margin-top:4px;font-variant-numeric:tabular-nums}
.chart{margin-top:16px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px}
.sec{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);
margin:22px 0 8px}
.log{background:#0b1016;border:1px solid var(--line);border-radius:10px;padding:10px 12px;
font-family:var(--mono);font-size:12px;line-height:1.7;max-height:320px;overflow:auto}
.log div{white-space:pre-wrap}.log .t{color:var(--accent);margin-right:8px}
.foot{margin-top:22px;color:var(--faint);font-size:11px}
"""


def _pull_live(host: str, port: int) -> tuple[list[tuple[float, float]], float | None]:
    """One IBKR connection, two reads: today's RTH SPY 1-min series AND the live account
    NetLiquidation (ground truth for the live P&L card). Main thread only. Returns
    ([(minute_since_open, close)], net_liq_or_None)."""
    from .data.ibkr_feed import IBKRFeed
    f = IBKRFeed(host=host, port=port, client_id=46, symbol="SPY")
    try:
        f.connect()
        net_liq = None
        try:
            for r in f.ib.accountSummary():
                if r.tag == "NetLiquidation":
                    net_liq = float(r.value)
                    break
        except Exception:
            pass
        bars = f.latest_bars(lookback_minutes=400)
        if bars.empty:
            return [], net_liq
        et = bars.index.tz_convert("America/New_York")
        pts = [((t.hour - 9) * 60 + t.minute - 30, float(c)) for t, c in zip(et, bars["close"])]
        return [p for p in pts if 0 <= p[0] <= 390], net_liq
    finally:
        try:
            f.disconnect()
        except Exception:
            pass


def _today_stats(db_path: str, day) -> dict:
    import sqlite3
    if not Path(db_path).exists():
        return {"n": 0, "pnl": 0.0, "open": 0}
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT pnl, closed_at FROM trades WHERE opened_at LIKE ?",
            (f"{day.isoformat()}%",)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return {"n": len(rows), "pnl": sum((r[0] or 0.0) for r in rows),
            "open": sum(1 for r in rows if r[1] is None)}


def build_html(db_path: str = "trades.db", log_dir: str = "logs", day=None,
               spy: list | None = None) -> str:
    day = day or datetime.now().date()
    spy = _STATE["spy"] if spy is None else spy
    log_path = Path(f"{log_dir}/daily_{day:%Y%m%d}.log")
    text = log_path.read_text() if log_path.exists() else ""
    events = parse_log_events(text) + trade_events(db_path, day)
    b = briefing(db_path)
    st = _today_stats(db_path, day)
    n_halt = sum(1 for _, t, _ in events if t == "halt")
    svg = render_svg(spy, events) if spy else (
        '<div style="color:#8894a3;padding:26px;text-align:center;font-family:ui-monospace">'
        'Waiting for the first SPY pull… (updates every ~60s)</div>')
    ec = {"🟢": "var(--good)", "🟠": "var(--warn)", "🔴": "var(--crit)"}.get(b["emoji"], "var(--good)")
    now_px = f"${spy[-1][1]:.2f}" if spy else "—"
    pnl = st["pnl"]
    # GROUND TRUTH: live account NetLiq vs yesterday's close = the real intraday P&L (ticks with
    # the account). The book "P&L today" is what the bot THINKS it made; this is what actually
    # happened — the two diverged by $314 on 2026-07-08 (phantom short). Never show book alone.
    netliq = _STATE.get("netliq")
    baseline = prior_netliq(read_netliq_ledger(f"{log_dir}/netliq.jsonl"), day)
    actual_pnl = (round(netliq - baseline["net_liq"], 2)
                  if (netliq is not None and baseline) else None)
    nl_txt = f"${netliq:,.0f}" if netliq is not None else "—"
    ap_txt = f"${actual_pnl:+,.0f}" if actual_pnl is not None else "—"
    ap_col = "var(--muted)" if actual_pnl is None else (
        "var(--good)" if actual_pnl >= 0 else "var(--crit)")
    logs = "".join(f'<div><span class="t">{t}</span>{msg}</div>'
                   for t, msg in tail_activity(text, 40)) or "<div>No activity logged yet.</div>"
    upd = _STATE["ts"].strftime("%H:%M:%S") if _STATE["ts"] else "—"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_S}"><title>ODTE-SPY-BOT — live</title>
<style>{_CSS}</style></head><body><div class="wrap">
  <div class="top"><span><b>ODTE-SPY-BOT</b> · live</span>
    <span class="live"><span class="d"></span>LIVE · auto-refresh {REFRESH_S}s · SPY updated {upd}</span></div>
  <div class="bl" style="border-left:3px solid {ec}"><span style="font-size:19px">{b['emoji']}</span>
    <div class="b"><b>{b['headline']}</b></div></div>
  <div class="grid">
    <div class="card"><div class="l">Account NetLiq</div><div class="v">{nl_txt}</div></div>
    <div class="card"><div class="l">Actual P&amp;L today</div><div class="v" style="color:{ap_col}">{ap_txt}</div></div>
    <div class="card"><div class="l">SPY now</div><div class="v">{now_px}</div></div>
    <div class="card"><div class="l">Trades today</div><div class="v">{st['n']}</div></div>
    <div class="card"><div class="l">Book P&amp;L today</div><div class="v" style="color:{'var(--good)' if pnl>=0 else 'var(--crit)'}">${pnl:,.0f}</div></div>
    <div class="card"><div class="l">Open now</div><div class="v">{st['open']}</div></div>
    <div class="card"><div class="l">Halts today</div><div class="v">{n_halt}</div></div>
  </div>
  <div class="sec">Today on SPY — events marked on the intraday tape</div>
  <div class="chart">{svg}</div>
  <div class="sec">Live activity log</div>
  <div class="log">{logs}</div>
  <div class="foot">Local live view (127.0.0.1) · read-only · paper trading · not financial advice.</div>
</div></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, "ok", "text/plain")
        try:
            html = build_html(self.server.db, self.server.logdir)  # type: ignore[attr-defined]
        except Exception as exc:
            html = f"<pre>render error: {exc}</pre>"
        self._send(200, html, "text/html; charset=utf-8")

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass  # keep it quiet


def _serve(port, db, logdir):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    srv.db, srv.logdir = db, logdir
    srv.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description="Live local dashboard for the running session")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default="trades.db")
    ap.add_argument("--logs", default="logs")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ibport", type=int, default=4002)
    ap.add_argument("--spy-every", type=int, default=60)
    a = ap.parse_args()
    threading.Thread(target=_serve, args=(a.port, a.db, a.logs), daemon=True).start()
    print(f"Live dashboard: http://127.0.0.1:{a.port}  (Ctrl-C to stop)")
    while True:                                   # SPY refresh on the MAIN thread (ib_insync)
        try:
            _STATE["spy"], _STATE["netliq"] = _pull_live(a.host, a.ibport)
        except Exception:
            pass
        _STATE["ts"] = datetime.now()
        time.sleep(a.spy_every)


if __name__ == "__main__":
    main()
