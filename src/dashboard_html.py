"""Self-contained HTML dashboard, GENERATED from trades.db (not hand-authored).

Same look as the shared status page, but every number is read live from the TradeLog so the
link never goes stale. Leads with the plain-English bottom line, then health tiles, the
pre-registered experiments, and the daily routine. Written EOD alongside the markdown
dashboard; purely read-only over trades.db (no trading impact).

    python -m src.dashboard_html --db trades.db --out docs/dashboard/status.html
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from .briefing import briefing
from .dashboard import _equity_svg, _rows
from .monitor import death_spiral_check
from .session_chart import build_session_svg
from .utils.holdout import ledger_status

_CSS = """
:root{--ink:#0e141b;--panel:#151d27;--line:#27323f;--text:#d8dee7;--muted:#8894a3;--faint:#5b6773;
--accent:#45c4b8;--accent-dim:#2c6f6a;--good:#49b65f;--warn:#d9a430;--crit:#e0533c;--idle:#7c8998;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--mono:ui-monospace,"SF Mono",Menlo,monospace}
*{box-sizing:border-box}
.wrap{max-width:1040px;margin:0 auto;padding:22px 20px 40px;color:var(--text);font-family:var(--sans);
line-height:1.5;background:radial-gradient(900px 380px at 88% -8%,rgba(69,196,184,.07),transparent 70%),var(--ink)}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--faint)}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;padding-bottom:16px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:11px}
.dot{width:9px;height:9px;border-radius:50%}
.brand b{font-family:var(--mono);font-size:15px}.brand span{color:var(--faint);font-family:var(--mono);font-size:12px}
.pill{font-family:var(--mono);font-size:12px;letter-spacing:.06em;padding:6px 12px;border-radius:999px;white-space:nowrap;
border:1px solid var(--accent-dim);color:var(--accent);background:rgba(69,196,184,.08)}
.bottomline{display:flex;gap:13px;align-items:flex-start;margin-top:18px;padding:15px 17px;border:1px solid var(--line);border-radius:11px}
.bottomline .e{font-size:20px;line-height:1.3}.bottomline .b{font-size:14.5px}
.bottomline .b b{color:var(--text)}.bottomline .do{display:block;margin-top:5px;font-family:var(--mono);font-size:12.5px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:15px 16px;display:flex;flex-direction:column;gap:9px;min-height:112px}
.card .top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.card .label{font-family:var(--mono);font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint)}
.val{font-family:var(--mono);font-size:26px;line-height:1.05;font-variant-numeric:tabular-nums}
.val.good{color:var(--good)}.val.idle{color:var(--muted)}.val.warn{color:var(--warn)}.val.crit{color:var(--crit)}
.sub{font-size:12.5px;color:var(--muted);margin-top:auto}
.chip{font-family:var(--mono);font-size:10.5px;letter-spacing:.05em;padding:3px 8px;border-radius:6px;white-space:nowrap}
.sec{margin-top:34px}.sec>.eyebrow{display:block;margin-bottom:12px}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--panel)}
table{width:100%;border-collapse:collapse;font-size:13.5px;min-width:520px}
th,td{text-align:left;padding:11px 14px;border-bottom:1px solid var(--line)}tr:last-child td{border-bottom:none}
th{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);font-weight:500}
td.h{font-family:var(--mono);color:var(--text)}td .n{font-family:var(--mono);color:var(--muted);font-variant-numeric:tabular-nums}
.lock{font-family:var(--mono);font-size:11px;color:var(--faint)}
.chart{margin-top:14px;border:1px solid var(--line);border-radius:12px;overflow:hidden}.chart img,.chart svg{display:block;width:100%}
.rail{display:flex;flex-direction:column;gap:2px}
.step{display:grid;grid-template-columns:70px 1fr;gap:14px;padding:9px 0;align-items:baseline}
.step .t{font-family:var(--mono);font-size:12px;color:var(--accent);text-align:right}
.step .d{border-left:1px solid var(--line);padding-left:16px}.step .d b{font-size:14px}.step .d span{display:block;color:var(--muted);font-size:12.5px}
footer{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);color:var(--faint);font-size:12px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
"""

_TIMELINE = [
    ("09:20", "Mac wakes", "Scheduled power-on, ready for the open."),
    ("09:25", "Pull → test-gate → health-check", "Updates code + models; refuses to run if tests fail or the broker login expired."),
    ("09:30<br>–15:30", "Trade defined-risk credit spreads", "Sells $5/$10 verticals, holds to a 50%-credit target — no premium stop. Skips illiquid or gapped markets."),
    ("15:55", "Flatten everything", "Nothing held overnight. 0DTE expires same day."),
    ("EOD", "Report → retrain → self-monitor → publish", "Logs fills, retrains the cost model, runs the death-spiral check, refreshes this page."),
    ("21:00<br>UTC", "Cloud retrain", "GitHub retrains models on fresh data — commits only if they improve."),
]


def _flag_style(flag: str, n: int) -> tuple[str, str, str]:
    """(pill text, semantic color var, dot color) from the death-spiral flag."""
    if flag == "RETIRE":
        return "RETIRE · STOP", "var(--crit)", "var(--crit)"
    if flag == "KILL_WATCH":
        return "KILL-WATCH", "var(--warn)", "var(--warn)"
    label = "ARMED · HEALTHY" if n == 0 else "TRADING · HEALTHY"
    return label, "var(--good)", "var(--good)"


def render_body(db_path: str = "trades.db") -> str:
    rows = _rows(db_path)
    b = briefing(db_path)
    ds = death_spiral_check(db_path)
    hs = ledger_status()
    n = len(rows)
    pnls = [r["pnl"] or 0.0 for r in rows]
    net = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    win_rate = (wins / n) if n else 0.0
    emoji_color = {"🟢": "var(--good)", "🟠": "var(--warn)", "🔴": "var(--crit)"}.get(b["emoji"], "var(--good)")
    pill, _sem, dot = _flag_style(ds["flag"], n)
    now = datetime.now().strftime("%Y-%m-%d %H:%M ET")

    # experiment counts (real)
    def cnt(pred):
        return sum(1 for r in rows if pred(r))
    w5, w10 = cnt(lambda r: (r.get("width") or 0) == 5), cnt(lambda r: (r.get("width") or 0) == 10)
    gexp, gexn = cnt(lambda r: (r.get("gex_net") or 0) > 0), cnt(lambda r: (r.get("gex_net") or 0) < 0)
    lim = cnt(lambda r: r.get("limit_exit") == 1)
    mkt = cnt(lambda r: r.get("limit_exit") == 0)
    pbf = cnt(lambda r: r.get("p_bad_fill") is not None)
    exp_rows = [
        ("H1", "IV vs realized-vol premium", str(n), "n ≥ 60/grp"),
        ("H2b", "$5 vs $10 spread width", f"$5:{w5} · $10:{w10}", "n ≥ 50/arm"),
        ("H3", "limit vs market exit fills", f"lim:{lim} · mkt:{mkt}", "n ≥ 50 limit"),
        ("H7", "dealer-gamma (GEX) regime", f"+:{gexp} · −:{gexn}", "n ≥ 60/grp"),
        ("H8", "market-implied touch-prob", str(n), "n ≥ 60/grp"),
        ("H9", "25-delta skew regime", str(n), "n ≥ 60/grp"),
        ("H10", "cost-quality meta-labeler", str(pbf), "train n ≥ 100 → holdout"),
        ("H4", "40% vs 60% profit target", "—", "queued"),
    ]
    exp_html = "".join(
        f'<tr><td class="h">{hid}</td><td>{what}</td><td class="n">{c}</td>'
        f'<td class="lock">{dec}</td></tr>' for hid, what, c, dec in exp_rows)

    tiles = [
        ("Live trades", str(n), "idle" if n == 0 else "good",
         "WAITING" if n == 0 else "LIVE", "var(--idle)" if n == 0 else "var(--good)",
         "First fills pending the next session." if n == 0 else f"{wins} winners so far."),
        ("Net P&amp;L (paper)", f"${net:,.0f}", "good" if net >= 0 else "crit",
         "PAPER", "var(--accent)", "Cumulative, paper account. Not real money."),
        ("Win rate", f"{win_rate:.0%}" if n else "—", "idle" if n == 0 else "good",
         f"n={n}", "var(--idle)", "Share of closed trades in profit."),
        ("Death-spiral monitor", "—" if ds["flag"] == "INSUFFICIENT" else f"{ds['ci_hi']:+.1f}",
         "idle" if ds["flag"] in ("INSUFFICIENT", "HEALTHY") else "warn",
         ds["flag"].replace("_", "-"), dot, "95% CI upper of $/trade. Auto-alerts if it turns."),
        ("Reserved holdout", hs["holdout"].split("..")[0][:7] + "…", "good",
         "UNTOUCHED" if hs["n_consumed"] == 0 else f"{hs['n_consumed']} USED", "var(--good)",
         "Proof data, locked in code. Never trained on."),
        ("Cost / month", "$29", "", "$29", "var(--accent)",
         "Polygon only. IBKR paper free · GitHub free · AWS not in use."),
    ]
    tiles_html = "".join(
        f'<div class="card"><div class="top"><span class="label">{lab}</span>'
        f'<span class="chip" style="color:{cc};background:rgba(124,137,152,.12)">{chip}</span></div>'
        f'<div class="val {vc}">{val}</div><div class="sub">{sub}</div></div>'
        for lab, val, vc, chip, cc, sub in tiles)

    chart = ""
    if n:
        chart = f'<div class="chart">{_equity_svg(pnls)}</div>'

    # Intraday SPY tape with the day's events (halts / gap guards / trades) marked on it.
    try:
        _sess = build_session_svg(db_path=db_path)
    except Exception:
        _sess = ""
    session_html = (
        '<section class="sec"><span class="eyebrow">Today on SPY — the bot&#39;s events on '
        'the intraday tape</span>'
        f'<div class="chart" style="background:var(--panel);padding:10px 12px 4px">{_sess}</div>'
        '</section>') if _sess else ""

    steps = "".join(
        f'<div class="step"><span class="t">{t}</span><div class="d"><b>{h}</b>'
        f'<span>{s}</span></div></div>' for t, h, s in _TIMELINE)

    return f"""<style>{_CSS}</style>
<div class="wrap">
  <div class="topbar">
    <div class="brand"><span class="dot" style="background:{dot}"></span>
      <b>ODTE-SPY-BOT</b><span>· system status</span></div>
    <span class="pill" style="border-color:{dot};color:{dot};background:rgba(69,196,184,.06)">{pill}</span>
  </div>

  <div class="bottomline" style="border-left:3px solid {emoji_color};background:linear-gradient(180deg,rgba(73,182,95,.05),transparent)">
    <span class="e" aria-hidden="true">{b['emoji']}</span>
    <div class="b"><b>{b['headline']}</b><br>{b['text'].splitlines()[1]}
      <span class="do" style="color:{emoji_color}">WHAT TO DO → {b['action']}</span></div>
  </div>

  <div class="grid">{tiles_html}</div>
  {chart}
  {session_html}

  <section class="sec"><span class="eyebrow">Pre-registered experiments — locked until their sample lands (no peeking)</span>
    <div class="tablewrap"><table>
      <thead><tr><th>Hypothesis</th><th>What it tests</th><th>Logged</th><th>Decides at</th></tr></thead>
      <tbody>{exp_html}</tbody></table></div>
  </section>

  <section class="sec"><span class="eyebrow">What it does every trading day — fully automated</span>
    <div class="rail">{steps}</div>
  </section>

  <footer><span>Paper trading only · not financial advice · 0DTE spreads can lose their full defined risk per trade.</span>
    <span class="eyebrow">generated {now}</span></footer>
</div>"""


def render_page(db_path: str = "trades.db") -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>ODTE-SPY-BOT — status</title></head><body style=\"margin:0;background:#0e141b\">"
            + render_body(db_path) + "</body></html>")


def generate(db_path: str = "trades.db", out_path: str = "docs/dashboard/status.html") -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_page(db_path))
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Generate the HTML status dashboard from trades.db")
    p.add_argument("--db", default="trades.db")
    p.add_argument("--out", default="docs/dashboard/status.html")
    a = p.parse_args()
    print(f"HTML dashboard written: {generate(a.db, a.out)}")


if __name__ == "__main__":
    main()
