"""Self-contained HTML status dashboard, GENERATED from the account + trade records.

One page, read-only, every number sourced from ground truth:
  * P&L and account value come from the **NetLiq ledger** (logs/netliq.jsonl) — the real IBKR
    paper-account balance recorded once per day. Day-over-day NetLiq change IS the P&L. This is
    the authoritative money number; the trade log is detail, not the headline.
  * Trades, halts and the SPY session tape come from trades.db + the session logs.

No external assets (works offline / inside a strict CSP). Written at EOD next to the markdown
dashboard.

    python -m src.dashboard_html --db trades.db --out docs/dashboard/status.html
"""
from __future__ import annotations

import argparse
import glob
import re
from datetime import date as _date
from datetime import datetime
from pathlib import Path

from .dashboard import _rows
from .monitor import death_spiral_check
from .reconcile import read_netliq_ledger
from .session_chart import build_session_svg, tail_activity
from .session_log import read_sessions

LEDGER = "logs/netliq.jsonl"

_CSS = """
:root{--ink:#0e141b;--panel:#151d27;--line:#27323f;--text:#d8dee7;--muted:#8894a3;--faint:#5b6773;
--accent:#45c4b8;--accent-dim:#2c6f6a;--good:#49b65f;--warn:#d9a430;--crit:#e0533c;--idle:#7c8998;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--mono:ui-monospace,"SF Mono",Menlo,monospace}
*{box-sizing:border-box}
.wrap{max-width:1000px;margin:0 auto;padding:24px 20px 48px;color:var(--text);font-family:var(--sans);
line-height:1.5;min-height:100vh;background:radial-gradient(900px 380px at 88% -8%,rgba(69,196,184,.06),transparent 70%),var(--ink)}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--faint)}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;padding-bottom:16px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:11px}
.dot{width:9px;height:9px;border-radius:50%}
.brand b{font-family:var(--mono);font-size:15px}.brand span{color:var(--faint);font-family:var(--mono);font-size:12px}
.pill{font-family:var(--mono);font-size:12px;letter-spacing:.06em;padding:6px 12px;border-radius:999px;white-space:nowrap;
border:1px solid var(--accent-dim);color:var(--accent);background:rgba(69,196,184,.08)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:15px 16px;display:flex;flex-direction:column;gap:8px;min-height:104px}
.card .top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.card .label{font-family:var(--mono);font-size:11px;letter-spacing:.11em;text-transform:uppercase;color:var(--faint)}
.val{font-family:var(--mono);font-size:25px;line-height:1.05;font-variant-numeric:tabular-nums}
.val.good{color:var(--good)}.val.idle{color:var(--text)}.val.warn{color:var(--warn)}.val.crit{color:var(--crit)}
.sub{font-size:12px;color:var(--muted);margin-top:auto}
.chip{font-family:var(--mono);font-size:10px;letter-spacing:.05em;padding:3px 8px;border-radius:6px;white-space:nowrap;color:var(--faint);background:rgba(124,137,152,.12)}
.sec{margin-top:34px}.sec>.eyebrow{display:block;margin-bottom:12px}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--panel)}
table{width:100%;border-collapse:collapse;font-size:13.5px;min-width:520px}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--line)}tr:last-child td{border-bottom:none}
th{font-family:var(--mono);font-size:10.5px;letter-spacing:.11em;text-transform:uppercase;color:var(--faint);font-weight:500}
td.h{font-family:var(--mono);color:var(--text)}td .n,td.n{font-family:var(--mono);font-variant-numeric:tabular-nums}
.pos{color:var(--good)}.neg{color:var(--crit)}.mut{color:var(--muted)}
.chart{margin-top:6px;border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:12px 14px}
.chart svg{display:block;width:100%}
.rail{display:flex;flex-direction:column;gap:2px}
.step{display:grid;grid-template-columns:66px 1fr;gap:14px;padding:8px 0;align-items:baseline}
.step .t{font-family:var(--mono);font-size:12px;color:var(--accent);text-align:right}
.step .d{border-left:1px solid var(--line);padding-left:16px}.step .d b{font-size:13.5px}.step .d span{display:block;color:var(--muted);font-size:12.5px}
footer{margin-top:36px;padding-top:16px;border-top:1px solid var(--line);color:var(--faint);font-size:12px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
.log{background:#0b1016;border:1px solid var(--line);border-radius:12px;padding:12px 14px;font-family:var(--mono);font-size:12px;line-height:1.7;max-height:280px;overflow:auto}
.log div{white-space:pre-wrap}.log .t{color:var(--accent);margin-right:8px}
.daytabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px;overflow-x:auto;padding-bottom:2px}
.daytab{font-family:var(--mono);font-size:12px;padding:6px 12px;border-radius:8px;cursor:pointer;background:var(--panel);color:var(--muted);border:1px solid var(--line);white-space:nowrap;transition:all .12s}
.daytab:hover{color:var(--text);border-color:var(--accent-dim)}
.daytab.active{color:#0e141b;background:var(--accent);border-color:var(--accent);font-weight:600}
.daycap{font-family:var(--mono);font-size:11.5px;color:var(--faint);margin:2px 2px 8px}
"""

def _account_svg(pts: list[tuple[str, float]]) -> str:
    """Account value (NetLiquidation) over time — the real equity curve. pts=[(date, net_liq)]."""
    if len(pts) < 2:
        return ""
    W, H, L, R, T, B = 720, 200, 66, 16, 18, 28
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.35 or max(abs(hi), 1.0) * 0.0005
    lo -= pad
    hi += pad
    span = (hi - lo) or 1.0
    n = len(pts) - 1

    def X(i):
        return L + (W - L - R) * i / n

    def Y(v):
        return H - B - (H - T - B) * (v - lo) / span

    line = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, (_, v) in enumerate(pts))
    up = vals[-1] >= vals[0]
    col = "#49b65f" if up else "#e0533c"
    area = (f'M{X(0):.1f},{H-B:.1f} L' + line.replace(" ", " L")
            + f' L{X(n):.1f},{H-B:.1f} Z')
    dots = "".join(f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="3" fill="{col}" '
                   f'stroke="#0e141b" stroke-width="1.5"><title>{d}: ${v:,.2f}</title></circle>'
                   for i, (d, v) in enumerate(pts))
    ylab = "".join(
        f'<text x="{L-8}" y="{Y(v)+3:.1f}" fill="#8894a3" font-size="10.5" text-anchor="end" '
        f'font-family="ui-monospace,Menlo,monospace">${v:,.0f}</text>'
        for v in ({vals[0], vals[-1], min(vals), max(vals)}))
    xlab = (f'<text x="{X(0):.1f}" y="{H-9}" fill="#5b6773" font-size="10.5" text-anchor="start" '
            f'font-family="ui-monospace,Menlo,monospace">{pts[0][0]}</text>'
            f'<text x="{X(n):.1f}" y="{H-9}" fill="#5b6773" font-size="10.5" text-anchor="end" '
            f'font-family="ui-monospace,Menlo,monospace">{pts[-1][0]}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;height:auto;display:block">'
            f'<path d="{area}" fill="{col}" fill-opacity="0.08"/>'
            f'<polyline points="{line}" fill="none" stroke="{col}" stroke-width="1.9"/>'
            f'{dots}{ylab}{xlab}</svg>')


def _kind(k: str) -> str:
    return {"bull_put": "Bull put", "bear_call": "Bear call"}.get(k or "", k or "—")


def _money(v, signed=False, cents=False):
    if v is None:
        return "—"
    dp = 2 if cents else 0
    return f"${v:+,.{dp}f}" if signed else f"${v:,.{dp}f}"


def render_body(db_path: str = "trades.db", live=None) -> str:
    """`live` is an optional reconcile.BrokerSnap. When present and available, the account value
    and open-position tiles come from the BROKER right now instead of the last EOD ledger row."""
    rows = _rows(db_path)
    ds = death_spiral_check(db_path)
    sessions = read_sessions()
    ledger = [e for e in read_netliq_ledger(LEDGER) if e.get("net_liq") is not None]
    ledger.sort(key=lambda e: (e.get("date", ""), e.get("ts", "")))
    now = datetime.now().strftime("%Y-%m-%d %H:%M ET")

    # --- account truth (the authoritative money numbers) --------------------------------
    net_liq = ledger[-1]["net_liq"] if ledger else None
    open_legs = int(ledger[-1].get("orphans") or 0) if ledger else 0
    acct_chip, live_pos = "IBKR PAPER", []
    if live is not None and getattr(live, "available", False) and live.net_liq is not None:
        net_liq = live.net_liq                       # broker truth beats the last EOD row
        live_pos = list(live.orphans or [])
        open_legs = len(live_pos)
        acct_chip = "LIVE"
    total_pnl = (round(net_liq - ledger[0]["net_liq"], 2)
                 if (ledger and net_liq is not None) else None)
    # per-day P&L = that day's NetLiq minus the previous tracked day's NetLiq
    daily: dict[str, float] = {}
    for i, e in enumerate(ledger):
        daily[e["date"]] = round(e["net_liq"] - ledger[i - 1]["net_liq"], 2) if i else 0.0

    # real closed trades: exclude the reconciled-unfilled bookkeeping rows AND unconfirmed-close
    # rows (pnl NULL — P&L unknown), matching briefing/monitor so the count agrees everywhere.
    trades = [r for r in rows if r.get("exit_reason") != "reconciled_unfilled"
              and r.get("pnl") is not None]
    n_closed = len(trades)
    # real trades per day — used everywhere a per-day count is shown, so the session table, the
    # tape caption and the trade log all agree (sessions.jsonl counts unfilled entries too).
    trades_by_day: dict[str, int] = {}
    for r in trades:
        d = (r.get("opened_at") or "")[:10]
        trades_by_day[d] = trades_by_day.get(d, 0) + 1

    health = {"RETIRE": ("Stopped", "crit", "var(--crit)", "RETIRED"),
              "KILL_WATCH": ("Watch", "warn", "var(--warn)", "WATCH")}.get(
                  ds["flag"], ("Healthy", "good", "var(--good)", "MONITORING"))
    hword, hcls, dot, hchip = health

    # --- tiles (pure data: label + value + one short factual chip, no prose) -----------
    pnl_cls = "idle" if total_pnl is None else ("good" if total_pnl >= 0 else "crit")
    since = ledger[0]["date"][5:].replace("-", "/") if ledger else None   # MM/DD
    tiles = [
        ("Account value", _money(net_liq, cents=True), "idle", acct_chip),
        ("Total P&amp;L", _money(total_pnl, signed=True, cents=True), pnl_cls,
         f"SINCE {since}" if since else "—"),
        ("Trades closed", str(n_closed), "idle", f"{len(sessions)} SESSIONS"),
        ("Open positions", str(open_legs), "good" if open_legs == 0 else "warn",
         "FLAT" if open_legs == 0 else "OPEN"),
        ("System", hword, hcls, hchip),
    ]
    tiles_html = "".join(
        f'<div class="card"><div class="top"><span class="label">{lab}</span>'
        f'<span class="chip">{chip}</span></div>'
        f'<div class="val {cls}">{val}</div></div>'
        for lab, val, cls, chip in tiles)

    # --- open positions right now (broker truth) ---------------------------------------
    openpos_html = ""
    if live_pos:
        tr = ""
        for o in live_pos:
            sec = o.get("secType", "OPT")
            what = (f"{o['localSymbol']}" if sec == "STK"
                    else f"{o['localSymbol']} {o.get('right','')} {o.get('strike',0):.0f}")
            tr += (f'<tr><td class="h">{sec}</td><td>{what}</td>'
                   f'<td class="n">{o["position"]:+d}</td>'
                   f'<td class="n">${o["avgCost"]:,.2f}</td></tr>')
        openpos_html = (
            '<section class="sec"><span class="eyebrow">Open positions — live, at the broker'
            '</span><div class="tablewrap"><table><thead><tr><th>Type</th><th>Contract</th>'
            '<th>Qty</th><th>Avg cost</th></tr></thead>'
            f'<tbody>{tr}</tbody></table></div></section>')

    # --- account value curve -----------------------------------------------------------
    acct_pts = [(e["date"], e["net_liq"]) for e in ledger]
    acct_html = ""
    if len(acct_pts) >= 2:
        acct_html = ('<section class="sec"><span class="eyebrow">Account value — real NetLiq, '
                     'one point per trading day</span>'
                     f'<div class="chart">{_account_svg(acct_pts)}</div></section>')

    # --- SPY session tape (pick any saved day) -----------------------------------------
    day_files = sorted(glob.glob("logs/spy_intraday_*.csv"), reverse=True)
    sbd = {s.get("date"): s for s in sessions}
    tabs, views = [], []
    for f in day_files:
        m = re.search(r"spy_intraday_(\d{4})(\d{2})(\d{2})\.csv", f)
        if not m:
            continue
        dy = _date(int(m[1]), int(m[2]), int(m[3]))
        try:
            svg = build_session_svg(day=dy, db_path=db_path)
        except Exception:
            svg = ""
        if not svg:
            continue
        dstr = dy.isoformat()
        first = not views
        s = sbd.get(dstr, {})
        dp = daily.get(dstr)
        cap = ((f'{trades_by_day.get(dstr, 0)} trades'
                + (f' · day P&L ${dp:+,.2f}' if dp is not None else '')
                + (f' · SPY ${s["spy_lo"]:.0f}–${s["spy_hi"]:.0f}' if s.get("spy_lo") else ''))
               if s else '')
        tabs.append(f'<button class="daytab{" active" if first else ""}" data-day="{dstr}" '
                    f'onclick="showDay(\'{dstr}\')">{dy:%a %b %-d}</button>')
        views.append(f'<div class="dayview" id="dv-{dstr}" '
                     f'style="display:{"block" if first else "none"}">'
                     f'<div class="daycap">{cap}</div>{svg}</div>')
    tape_html = (
        '<section class="sec"><span class="eyebrow">SPY session tape — select a day</span>'
        '<div class="daytabs">' + "".join(tabs) + '</div>'
        '<div class="chart">' + "".join(views) + '</div></section>') if views else ""

    # --- session history ---------------------------------------------------------------
    hist_html = ""
    if sessions:
        def day_pnl(s):
            # Account truth only. A day with a ledger delta shows it; a no-trade day is $0;
            # otherwise "—". NEVER fall back to book net_pnl (that reintroduces the overclaim
            # the ledger exists to prevent — e.g. book +$156 on a day the account fell).
            d = s.get("date")
            if d in daily:
                return daily[d]
            return 0.0 if trades_by_day.get(d, 0) == 0 else None
        tr = ""
        for s in sessions:
            dp = day_pnl(s)
            dp_txt = ("—" if dp is None else
                      f'<span class="{"pos" if dp >= 0 else "neg"}">${dp:+,.2f}</span>')
            rng = ("$%.0f–$%.0f" % (s["spy_lo"], s["spy_hi"])) if s.get("spy_lo") else "—"
            tr += (f'<tr><td class="h">{s.get("date","")}</td>'
                   f'<td class="n">{trades_by_day.get(s.get("date"), 0)}</td>'
                   f'<td class="n">{dp_txt}</td>'
                   f'<td class="n">{s.get("halts",0)}</td>'
                   f'<td class="n mut">{rng}</td></tr>')
        hist_html = (
            '<section class="sec"><span class="eyebrow">Daily history</span>'
            '<div class="tablewrap"><table><thead><tr><th>Date</th><th>Trades</th>'
            '<th>Day P&amp;L</th><th>Halts</th><th>SPY range</th></tr></thead>'
            f'<tbody>{tr}</tbody></table></div></section>')

    # --- trade log ---------------------------------------------------------------------
    trades_html = ""
    if trades:
        tr = ""
        for r in reversed(trades[-25:]):
            opened = (r.get("opened_at") or "")[:16].replace("T", " ")
            credit = r.get("credit_fill") if r.get("credit_fill") is not None else r.get("credit_est")
            strikes = (f'{r.get("short_strike"):.0f}/{r.get("long_strike"):.0f}'
                       if r.get("short_strike") is not None else "—")
            pnl = r.get("pnl")
            pnl_txt = ("—" if pnl is None else
                       f'<span class="{"pos" if pnl >= 0 else "neg"}">${pnl:+,.2f}</span>')
            tr += (f'<tr><td class="h">{opened}</td><td>{_kind(r.get("kind"))}</td>'
                   f'<td class="n">{strikes}</td>'
                   f'<td class="n">{("$%.2f" % credit) if credit is not None else "—"}</td>'
                   f'<td class="mut">{(r.get("exit_reason") or "—").replace("_"," ")}</td>'
                   f'<td class="n">{pnl_txt}</td></tr>')
        trades_html = (
            '<section class="sec"><span class="eyebrow">Trade log — closed positions</span>'
            '<div class="tablewrap"><table><thead><tr><th>Opened</th><th>Type</th>'
            '<th>Strikes</th><th>Credit</th><th>Exit</th><th>P&amp;L</th></tr></thead>'
            f'<tbody>{tr}</tbody></table></div></section>')

    # --- activity log (compact) --------------------------------------------------------
    try:
        lp = Path(f"logs/daily_{datetime.now():%Y%m%d}.log")
        acts = tail_activity(lp.read_text(), 14) if lp.exists() else []
    except Exception:
        acts = []
    log_html = ('<section class="sec"><span class="eyebrow">Recent activity</span>'
                '<div class="log">' + "".join(
                    f'<div><span class="t">{t}</span>{msg}</div>' for t, msg in acts)
                + '</div></section>') if acts else ""

    return f"""<style>{_CSS}</style>
<div class="wrap">
  <div class="topbar">
    <div class="brand"><span class="dot" style="background:{dot}"></span>
      <b>ODTE-SPY-BOT</b><span>· paper trading status</span></div>
    <span class="pill" style="border-color:{dot};color:{dot};background:rgba(69,196,184,.06)">{hword.upper()}</span>
  </div>

  <div class="grid">{tiles_html}</div>
  {openpos_html}
  {acct_html}
  {tape_html}
  {hist_html}
  {trades_html}
  {log_html}

  <footer><span>Paper trading · not financial advice</span>
    <span class="eyebrow">updated {now}</span></footer>
</div>
<script>
function showDay(d){{
  var vs=document.querySelectorAll('.dayview');
  for(var i=0;i<vs.length;i++){{vs[i].style.display='none';}}
  var el=document.getElementById('dv-'+d);
  if(el){{el.style.display='block';}}
  var ts=document.querySelectorAll('.daytab');
  for(var j=0;j<ts.length;j++){{ts[j].classList.toggle('active', ts[j].getAttribute('data-day')===d);}}
}}
</script>"""


def render_page(db_path: str = "trades.db", live=None) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>ODTE-SPY-BOT — status</title></head>"
            "<body style=\"margin:0;background:#0e141b\">"
            + render_body(db_path, live=live) + "</body></html>")


def generate(db_path: str = "trades.db", out_path: str = "docs/dashboard/status.html",
             live=None) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_page(db_path, live=live))
    return out


def _live_snapshot(host: str, port: int, symbol: str):
    """Read-only broker snapshot for the account/open-position tiles. Fail-soft: on any error
    the dashboard falls back to the last EOD ledger row."""
    try:
        from .reconcile import broker_snapshot
        return broker_snapshot(host, port, symbol, client_id=57)
    except Exception:
        return None


def main() -> None:
    p = argparse.ArgumentParser(description="Generate the HTML status dashboard")
    p.add_argument("--db", default="trades.db")
    p.add_argument("--out", default="docs/dashboard/status.html")
    p.add_argument("--live", action="store_true",
                   help="read the account + open positions from IBKR instead of the last "
                        "EOD ledger row (read-only; falls back if the Gateway is down)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002)
    p.add_argument("--symbol", default="SPY")
    a = p.parse_args()
    live = _live_snapshot(a.host, a.port, a.symbol) if a.live else None
    if a.live:
        print("live broker snapshot:",
              "OK" if (live and live.available) else "unavailable (using EOD ledger)")
    print(f"HTML dashboard written: {generate(a.db, a.out, live=live)}")


if __name__ == "__main__":
    main()
