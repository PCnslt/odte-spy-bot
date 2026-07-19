"""War-room dashboard — ONE live view of the bot, served where the data lives (this Mac).

Why not GitHub Codespaces (the advisor's spec): the IB Gateway is 127.0.0.1:4002 on THIS
machine, and every state file (trades.db, logs/netliq.jsonl, risk_state.json, quotes/) is
deliberately gitignored — financial data never enters the repo. A Codespace would show an
empty shell, and the free tier is not always-on hosting anyway. The remote read-only view
remains the claude.ai artifact snapshot.

Design: stdlib only (no new deps). A background thread refreshes broker truth every ~20s;
requests render instantly from the cached state. VIEW-ONLY by owner order (2026-07-20):
this page can never alter the bot — no POST, no control endpoints, no flag files. Bot
control is terminal-only (config + python -m src.main --flatten).

    python dashboard/warroom.py            # serve http://127.0.0.1:8090
    python dashboard/warroom.py --once     # print one rendered page (smoke test)
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

PORT = 8090
REFRESH_S = 15                     # page auto-refresh
BROKER_POLL_S = 20                 # background broker poll
TEST_GATE = REPO / "logs" / "test_gate.txt"


# --- tiny readers (pure, testable) ----------------------------------------------------------
def read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if ln:
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return rows[-limit:] if limit else rows


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def todays_trades(db: Path, day: str) -> list[dict]:
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id,kind,opened_at,closed_at,exit_reason,pnl,quantity,short_strike,"
            "long_strike,credit_fill FROM trades WHERE opened_at LIKE ? ORDER BY id",
            (f"{day}%",))]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def tail_actions(log: Path, n: int = 20) -> list[tuple[str, str]]:
    """Last n substantive bot actions from the daily log (entries/exits/halts/alerts)."""
    if not log.exists():
        return []
    pat = re.compile(r"(\d{2}:\d{2}:\d{2}).*?(?:alert: \[\w+\] |\| )(.*)")
    keep = re.compile(r"OPEN |CLOSE |STRIKE DEFENSE|flatten|halt|ANOMALY|UNMANAGED|ORPHAN|"
                      r"Skipped|entries|GAP GUARD|Session|CRITICAL|unfilled|cancelled",
                      re.IGNORECASE)
    out = []
    for ln in log.read_text(errors="replace").splitlines():
        if "ib_insync" in ln or not keep.search(ln):
            continue
        m = pat.search(ln)
        if m:
            out.append((m.group(1), m.group(2).strip()[:160]))
    return out[-n:]


def sev(ok: bool | None, warn: bool = False) -> str:
    """Status class: green/amber/red/grey."""
    if ok is None:
        return "na"
    if ok:
        return "ok"
    return "warn" if warn else "crit"


def fmt_money(v, cents=True):
    if v is None:
        return "—"
    return f"${v:,.2f}" if cents else f"${v:,.0f}"


# --- state assembly -------------------------------------------------------------------------
class State:
    """Cached broker + file state; broker refreshed by a daemon thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.broker = None            # reconcile.BrokerSnap or None
        self.broker_ts = 0.0
        self.vrp = {}                 # {"iv":..,"rv":..,"vix":..,"ts":..}

    def refresh_broker(self):
        try:
            from src.reconcile import broker_snapshot
            b = broker_snapshot("127.0.0.1", 4002, "SPY", client_id=58)
        except Exception:
            b = None
        with self.lock:
            self.broker = b
            self.broker_ts = time.time()

    def refresh_vrp(self):
        """Best-effort IV/RV/VIX telemetry from existing entitlements. Honest n/a on failure."""
        out = {}
        try:
            from src.utils.config import load_config
            from src.data.polygon_options import PolygonOptions
            from datetime import date, timedelta
            cfg = load_config()
            poly = PolygonOptions.from_config(cfg)
            spy = poly.stock_history(date.today() - timedelta(days=45), date.today())
            if len(spy) >= 21:
                import math
                closes = list(spy["close"])[-21:]
                rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
                mu = sum(rets) / len(rets)
                var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
                out["rv20"] = round((var ** 0.5) * (252 ** 0.5) * 100, 2)
                spot = float(closes[-1])
                tick = poly.option_ticker(round(spot), "P", date.today())
                snap = poly.contract_snapshot(tick)
                if snap.get("iv"):
                    out["iv_atm"] = round(float(snap["iv"]) * 100, 2)
        except Exception:
            pass
        with self.lock:
            self.vrp = {**out, "ts": time.time()}

    def snapshot(self) -> dict:
        with self.lock:
            broker, broker_age = self.broker, time.time() - self.broker_ts
            vrp = dict(self.vrp)
        day = datetime.now().strftime("%Y-%m-%d")
        led = read_jsonl(REPO / "logs" / "netliq.jsonl")
        rs = read_json(REPO / "logs" / "risk_state.json")
        log = REPO / "logs" / f"daily_{day.replace('-', '')}.log"
        qdir = REPO / "logs" / "quotes"
        qfiles = sorted(qdir.glob("*.csv.gz")) if qdir.exists() else []
        qfresh = (time.time() - qfiles[-1].stat().st_mtime) < 180 if qfiles else False
        heartbeat = (time.time() - log.stat().st_mtime) < 120 if log.exists() else False
        test_line = TEST_GATE.read_text().strip() if TEST_GATE.exists() else ""
        # G2-FORWARD progress (docs/PREREGISTRATION_G2FWD.md): sessions = archive days;
        # trades toward the gate = registered-structure rounds (0 until that strategy trades);
        # basis fills = real fills usable by scripts/basis.py.
        sessions = len({f.name[:8] for f in qfiles if not f.name.endswith("_snap.csv.gz")})
        snaps = len([f for f in qfiles if f.name.endswith("_snap.csv.gz")])
        basis_fills = 0
        try:
            import sqlite3
            c = sqlite3.connect(REPO / "trades.db")
            basis_fills = c.execute("SELECT COUNT(*) FROM trades "
                                    "WHERE exit_cost_fill IS NOT NULL").fetchone()[0]
            c.close()
        except Exception:
            pass
        return {
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S ET"),
            "day": day, "broker": broker, "broker_age": broker_age, "vrp": vrp,
            "ledger": led, "risk": rs, "trades": todays_trades(REPO / "trades.db", day),
            "actions": tail_actions(log),
            "logger_fresh": qfresh, "heartbeat": heartbeat, "test_gate": test_line,
            "log_exists": log.exists(),
            "g2fwd": {"sessions": sessions, "snap_days": snaps, "basis_fills": basis_fills},
        }


# --- rendering (pure) -----------------------------------------------------------------------
CSS = """
:root{--bg:#0e141b;--panel:#151d27;--line:#27323f;--txt:#d8dee7;--mut:#8894a3;
--ok:#49b65f;--warn:#d9a430;--crit:#e0533c;--na:#5b6773;--acc:#45c4b8;
--mono:ui-monospace,Menlo,monospace;--sans:system-ui,-apple-system,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font-family:var(--sans);font-size:17px;line-height:1.5}
.wrap{max-width:1100px;margin:0 auto;padding:18px 16px 60px}
h1{font-family:var(--mono);font-size:20px;margin:0}
.top{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;
border-bottom:2px solid var(--line);padding-bottom:12px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h2{font-family:var(--mono);font-size:12px;letter-spacing:.12em;text-transform:uppercase;
color:var(--mut);margin:0 0 10px}
.kv{display:flex;justify-content:space-between;gap:10px;padding:5px 0;font-size:17px}
.kv b{font-family:var(--mono);font-variant-numeric:tabular-nums;font-weight:600}
.big{font-size:30px}.ok{color:var(--ok)}.warn{color:var(--warn)}.crit{color:var(--crit)}
.na{color:var(--na)}
.dot{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:8px;
vertical-align:baseline}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.crit{background:var(--crit)}
.dot.na{background:var(--na)}
.log{font-family:var(--mono);font-size:12.5px;line-height:1.75;max-height:330px;
overflow:auto;background:#0b1016;border-radius:8px;padding:10px 12px}
.log .t{color:var(--acc);margin-right:8px}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:13px}
td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
th{color:var(--na);font-size:10.5px;text-transform:uppercase;letter-spacing:.1em}
.arch{display:flex;flex-direction:column;gap:2px;margin-top:6px}
.arow{display:flex;gap:8px;flex-wrap:wrap}
.an{flex:1 1 240px;border:1px solid var(--line);border-left:4px solid var(--na);
border-radius:8px;padding:8px 10px;background:var(--bg)}
.an b{display:block;font-family:var(--mono);font-size:14px}
.an span{color:var(--mut);font-size:13px}
.an.ok{border-left-color:var(--ok)}.an.warn{border-left-color:var(--warn)}
.an.crit{border-left-color:var(--crit)}
.aflow{color:var(--na);text-align:center;font-size:12px;line-height:1.2}
.note{color:var(--mut);font-size:13px;margin-top:8px}
"""


def render(s: dict) -> str:
    b = s["broker"]
    live = bool(b and getattr(b, "available", False))
    led = s["ledger"]
    net = (b.net_liq if live and b.net_liq else (led[-1]["net_liq"] if led else None))
    base = led[0]["net_liq"] if led else None
    total = round(net - base, 2) if (net is not None and base is not None) else None
    prev = led[-1]["net_liq"] if (live and led) else (led[-2]["net_liq"] if len(led) > 1 else None)
    day_pnl = round(net - prev, 2) if (net is not None and prev is not None) else None
    rs, vrp = s["risk"], s["vrp"]
    budget = 100_000.0
    realized = float(rs.get("realized_pnl_today", 0.0) or 0.0)
    halt_pct = -realized / (0.02 * budget) * 100 if realized < 0 else 0.0
    trades_left = max(0, 4 - int(rs.get("trades_today", 0) or 0))
    open_pos = list(getattr(b, "orphans", []) or []) if live else []
    iv, rv = vrp.get("iv_atm"), vrp.get("rv20")
    vrp_pts = round(iv - rv, 2) if (iv is not None and rv is not None) else None

    def kv(label, val, cls="", big=False):
        return (f'<div class="kv"><span>{label}</span>'
                f'<b class="{cls} {"big" if big else ""}">{val}</b></div>')

    # account
    acct = kv("Account value", fmt_money(net), "", True)
    acct += kv("Day P&L", fmt_money(day_pnl), sev(day_pnl is not None and day_pnl >= 0,
               warn=day_pnl is not None and day_pnl < 0) if day_pnl is not None else "na")
    acct += kv("Total P&L (vs $1M)", fmt_money(total),
               "ok" if (total or 0) >= 0 else "crit")
    acct += kv("Cash", fmt_money(getattr(b, "cash", None)) if live else "—")
    acct += kv("Source", "LIVE broker" if live else "last EOD ledger",
               "ok" if live else "warn")

    # positions
    if open_pos:
        rows = "".join(f"<tr><td>{p.get('secType','')}</td><td>{p.get('localSymbol','')}</td>"
                       f"<td>{p.get('position','')}</td><td>{p.get('avgCost','')}</td></tr>"
                       for p in open_pos)
        pos = (f'<table><tr><th>Type</th><th>Contract</th><th>Qty</th><th>Avg cost</th></tr>'
               f'{rows}</table>'
               + kv("Unrealized", fmt_money(getattr(b, "unrealized_pnl", None)),
                    "crit" if (getattr(b, "unrealized_pnl", 0) or 0) < 0 else "ok"))
    else:
        pos = kv("Open positions", "FLAT" if live else "— (broker offline)",
                 "ok" if live else "na", True)
    for t in s["trades"]:
        st = "open" if not t["closed_at"] else (t["exit_reason"] or "closed")
        pos += kv(f'{t["kind"]} {t["opened_at"][11:16]}',
                  f'{st} {"" if t["pnl"] is None else fmt_money(t["pnl"])}')

    # risk
    risk = kv("Entries", "risk-manager gated", "ok")
    risk += kv("Daily-loss halt use", f"{halt_pct:.0f}% of −$2,000",
               sev(halt_pct < 60, warn=halt_pct < 100))
    risk += kv("Halted", str(bool(rs.get("halted", False))),
               "crit" if rs.get("halted") else "ok")
    risk += kv("Trades left today", str(trades_left), "ok" if trades_left else "warn")
    risk += kv("Consecutive losses", str(rs.get("consecutive_losses", 0)),
               sev(int(rs.get("consecutive_losses", 0) or 0) < 4, warn=True))
    risk += kv("VRP (IV−RV, 20d)", f"{vrp_pts:+.1f} pts" if vrp_pts is not None else "n/a",
               "ok" if (vrp_pts or 0) >= 2 else "na")
    risk += kv("ATM IV / RV20", f'{iv or "–"} / {rv or "–"}', "na")

    # health
    hlth = kv("IB Gateway", "connected" if live else "unreachable",
              sev(live, warn=not s["log_exists"]))
    hlth += kv("Bot heartbeat", "active" if s["heartbeat"] else
               ("no session today" if not s["log_exists"] else "STALE >2min"),
               sev(s["heartbeat"], warn=not s["log_exists"]))
    hlth += kv("Quote logger", "recording" if s["logger_fresh"] else "idle",
               sev(s["logger_fresh"], warn=True))
    tg = s["test_gate"]
    hlth += kv("Test gate", tg or "unknown",
               "ok" if "PASS" in tg else ("crit" if "FAIL" in tg else "na"))
    hlth += kv("Broker data age", f'{int(s["broker_age"])}s', "na")
    g2f = s.get("g2fwd", {})
    hlth += kv("G2-FWD sessions", f'{g2f.get("sessions", 0)} / 60', "na")
    hlth += kv("G2-FWD trades", "0 / 200 (structure not yet trading)", "na")
    hlth += kv("G2-FWD basis fills", f'{g2f.get("basis_fills", 0)} / 40', "na")
    hlth += kv("VRP snap days", f'{g2f.get("snap_days", 0)} (15:50 + 09:35 marks)', "na")

    acts = "".join(f'<div><span class="t">{t}</span>{html.escape(msg)}</div>'
                   for t, msg in s["actions"]) or '<div class="na">no actions yet today</div>'

    # architecture map — live module states from the same signals the panels use
    def node(name, detail, state):
        return (f'<div class="an {state}"><b>{name}</b><span>{detail}</span></div>')
    gw = "ok" if live else "crit"
    arch = (
        '<div class="arch">'
        '<div class="arow">'
        + node("Polygon Starter", "history · IV/GEX telemetry", "ok")
        + node("IB Gateway :4002", "delayed quotes (15m) · orders · truth", gw)
        + '</div><div class="aflow">▼</div><div class="arow">'
        + node("Live loop (src/main.py)", "30s poll: signal → risk → order state machine",
               "ok" if s["heartbeat"] else "warn")
        + node("Quote logger (id 49)", "XSP chain 1/min + 15:50/09:35 snaps → logs/quotes/",
               "ok" if s["logger_fresh"] else "warn")
        + '</div><div class="aflow">▼</div><div class="arow">'
        + node("Broker truth", "ib.positions() arbiter · orphan sweep · confirm-flat", gw)
        + node("Risk state", "daily halt · trades/day cap · consec-loss brake",
               "crit" if rs.get("halted") else "ok")
        + '</div><div class="aflow">▼</div><div class="arow">'
        + node("trades.db + ledger", "fills (broker-truth) · netliq.jsonl (EOD reconcile)", "ok")
        + node("War room :8090", "this page · reads files + broker snapshot",
               "ok")
        + '</div><div class="aflow">▼</div><div class="arow">'
        + node("Entry gate", "risk manager + config only; dashboard is view-only", "ok")
        + node("Gates G1.5 → G2-FWD", "sealed kill-screens; logger archive feeds them",
               "ok" if "PASS" in tg else ("crit" if "FAIL" in tg else "na"))
        + '</div></div>')

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_S}">
<title>ODTE war room</title><style>{CSS}</style></head><body><div class="wrap">
<div class="top"><h1><span class="dot {'ok' if live else 'warn'}"></span>ODTE-SPY-BOT · WAR ROOM</h1>
<span style="font-family:var(--mono);color:var(--mut)">{s['now']} · refresh {REFRESH_S}s</span></div>
<div class="grid">
<div class="card"><h2>Account</h2>{acct}</div>
<div class="card"><h2>Positions & today's trades</h2>{pos}</div>
<div class="card"><h2>Risk</h2>{risk}</div>
<div class="card"><h2>System health</h2>{hlth}</div>
</div>
<div class="card" style="margin-top:14px"><h2>Architecture — live module map</h2>{arch}</div>
<div class="card" style="margin-top:14px"><h2>Recent activity</h2><div class="log">{acts}</div></div>
<div class="card" style="margin-top:14px"><h2>Next milestones</h2>
<p class="note">XSP rehearsal (separate trades_rehearsal.db): target Mon 2026-07-27 ·
G1.5 harness ~07-27, first run after ≥10 logger sessions (~08-07) ·
G2-FWD earliest eligibility: 60 sessions / 200 structure trades / 40 basis fills (~Q4 2026).
This dashboard is VIEW-ONLY — bot control is terminal-only by owner order (2026-07-20).</p>
</div>
</div></body></html>"""


# --- server ---------------------------------------------------------------------------------
STATE = State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):                       # quiet
        pass

    def _send(self, code: int, body: str, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, render(STATE.snapshot()))

    # NO do_POST: this dashboard is VIEW-ONLY by owner order (2026-07-20). It must never be
    # able to alter the bot — control is terminal-only (python -m src.main --flatten, config).


def _poller():                                       # pragma: no cover
    while True:
        STATE.refresh_broker()
        if time.time() - STATE.vrp.get("ts", 0) > 300:
            STATE.refresh_vrp()
        time.sleep(BROKER_POLL_S)


def main():                                          # pragma: no cover
    if "--once" in sys.argv:
        STATE.refresh_broker()
        print(render(STATE.snapshot())[:4000])
        return
    threading.Thread(target=_poller, daemon=True).start()
    print(f"war room: http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":                           # pragma: no cover
    main()
