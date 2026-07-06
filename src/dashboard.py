"""Dashboard generator: trades.db + config -> docs/dashboard/index.md (+ equity.svg).

Runs at the end of every session (wired into run_paper_day.sh) and is committed/pushed so
the dashboard lives IN the GitHub repo — viewable anywhere, no Mac required, no new
infrastructure, no secrets exposed. Markdown renders natively on github.com.

HARKing discipline: the dashboard shows LIVE (pre-registered) evidence only. Historical
exploration results live in docs/AI_REVIEW.md and are linked, clearly labeled, never mixed.

    python -m src.dashboard              # writes docs/dashboard/index.md
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

OUT_DIR = Path("docs/dashboard")

# Month-1 thresholds from docs/RESEARCH_PROTOCOL.md (mirrored here for display only).
SLIP_V1, SLIP_V3 = 0.05, 0.15


def _rows(db_path: str) -> list[dict]:
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE closed_at IS NOT NULL ORDER BY opened_at")]
    conn.close()
    return rows


def _fmt(x, spec=".2f", none="—"):
    return none if x is None else format(x, spec)


def _equity_svg(pnls: list[float]) -> str:
    """Dependency-free cumulative-P&L polyline."""
    W, H, PAD = 720, 220, 30
    eq, run = [0.0], 0.0
    for p in pnls:
        run += p
        eq.append(run)
    lo, hi = min(eq), max(eq)
    span = (hi - lo) or 1.0
    n = len(eq) - 1 or 1
    pts = " ".join(
        f"{PAD + (W - 2 * PAD) * i / n:.1f},{H - PAD - (H - 2 * PAD) * (v - lo) / span:.1f}"
        for i, v in enumerate(eq))
    zero_y = H - PAD - (H - 2 * PAD) * (0 - lo) / span
    color = "#2da44e" if eq[-1] >= 0 else "#cf222e"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}"
  viewBox="0 0 {W} {H}" style="background:#fff">
  <line x1="{PAD}" y1="{zero_y:.1f}" x2="{W - PAD}" y2="{zero_y:.1f}"
        stroke="#d0d7de" stroke-dasharray="4 3"/>
  <polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>
  <text x="{PAD}" y="16" font-family="monospace" font-size="12" fill="#57606a">
    cumulative P&amp;L: ${eq[-1]:.2f} over {len(pnls)} trades</text>
</svg>"""


def _slip_stats(rows: list[dict], key: str, flt=lambda r: True) -> tuple[int, float | None]:
    vals = [r[key] for r in rows if flt(r) and r.get(key) is not None]
    return len(vals), (sum(vals) / len(vals) if vals else None)


def _bucket(mean: float | None) -> str:
    if mean is None:
        return "no data"
    if mean <= SLIP_V1:
        return "**v1-ish (optimistic fills confirmed)**"
    if mean >= SLIP_V3:
        return "**v3-ish (pessimistic fills confirmed)**"
    return "between models"


def generate(db_path: str = "trades.db", out_dir: Path = OUT_DIR) -> Path:
    rows = _rows(db_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M ET")

    md: list[str] = []
    md.append("# odte-spy-bot — Live Dashboard")
    md.append(f"\n*Generated {now} · auto-updated at each session close · "
              "**LIVE pre-registered evidence only** — historical exploration lives in "
              "[AI_REVIEW.md](../AI_REVIEW.md) and is never mixed into this page.*\n")
    md.append("> Context: the historical harness says this strategy is NEGATIVE under "
              "pessimistic fill assumptions (~−$8.6/trade). The open question this page "
              "answers over time: **do real fills beat that model?**\n")

    # --- headline stats ---
    n = len(rows)
    md.append("## Live results (paper)\n")
    if n == 0:
        md.append("_No closed trades yet. Zero trades on a given day is normal — the "
                  "entry gates are strict._\n")
    else:
        pnls = [r["pnl"] or 0.0 for r in rows]
        wins = sum(1 for p in pnls if p > 0)
        gw = sum(p for p in pnls if p > 0)
        gl = -sum(p for p in pnls if p < 0)
        pf = (gw / gl) if gl > 0 else float("inf")
        md.append(f"| Trades | Win rate | Profit factor | $/trade | Total P&L |")
        md.append(f"|---|---|---|---|---|")
        md.append(f"| {n} | {wins / n:.1%} | {pf:.2f} | ${sum(pnls) / n:.2f} "
                  f"| ${sum(pnls):.2f} |\n")
        (out_dir / "equity.svg").write_text(_equity_svg(pnls))
        md.append("![equity curve](equity.svg)\n")
        if n < 30:
            md.append(f"_n={n} < 30 — treat every number above as noise._\n")

    # --- fill quality: THE decisive evidence ---
    md.append("## Fill quality — the decisive evidence\n")
    md.append("| Metric | n | Mean | Verdict (protocol thresholds) |")
    md.append("|---|---|---|---|")
    en_n, en_m = _slip_stats(rows, "entry_slippage")
    md.append(f"| Entry slippage (est − fill) | {en_n} | {_fmt(en_m, '.3f')} "
              f"| {_bucket(en_m)} |")
    xl_n, xl_m = _slip_stats(rows, "exit_slippage", lambda r: r.get("limit_exit") == 1)
    md.append(f"| Exit slippage — limit | {xl_n} | {_fmt(xl_m, '.3f')} | {_bucket(xl_m)} |")
    xm_n, xm_m = _slip_stats(rows, "exit_slippage", lambda r: r.get("limit_exit") == 0)
    md.append(f"| Exit slippage — market | {xm_n} | {_fmt(xm_m, '.3f')} | {_bucket(xm_m)} |")
    md.append("")

    # --- experiment progress (pre-registered) ---
    md.append("## Pre-registered experiments — progress toward decision n\n")
    md.append("| Hypothesis | Groups (n so far) | Decision at |")
    md.append("|---|---|---|")
    w5 = sum(1 for r in rows if (r.get("width") or 0) == 5)
    w10 = sum(1 for r in rows if (r.get("width") or 0) == 10)
    md.append(f"| H2b width A/B | $5: {w5} · $10: {w10} | ≥50/arm |")
    ivrv_hi = sum(1 for r in rows if r.get("iv_short") and r.get("rv_60m")
                  and r["iv_short"] > 1.2 * r["rv_60m"])
    ivrv_lo = sum(1 for r in rows if r.get("iv_short") and r.get("rv_60m")
                  and r["iv_short"] <= 1.2 * r["rv_60m"])
    md.append(f"| H1 IV/RV | IV>1.2×RV: {ivrv_hi} · rest: {ivrv_lo} | ≥60/group |")
    md.append(f"| H3 limit-vs-market exits | limit: {xl_n} · market: {xm_n} | ≥50 limit |")
    gexp = sum(1 for r in rows if (r.get("gex_net") or 0) > 0)
    gexn = sum(1 for r in rows if (r.get("gex_net") or 0) < 0)
    md.append(f"| H7 GEX regime | GEX+: {gexp} · GEX−: {gexn} | ≥60/group |")
    pt_n = sum(1 for r in rows if r.get("prob_touch") is not None)
    sk_n = sum(1 for r in rows if r.get("skew_25d") is not None)
    md.append(f"| H8 touch-prob EV | logged: {pt_n} | ≥60/group |")
    md.append(f"| H9 skew regime | logged: {sk_n} | ≥60/group |")
    md.append(f"| H4 profit target | queued (starts after H2b) | — |")
    md.append("")

    # --- recent trades ---
    if rows:
        md.append("## Recent trades (last 15)\n")
        md.append("| Opened | Kind | W | Credit est→fill | Exit | Cost est→fill "
                  "| P&L | GEX |")
        md.append("|---|---|---|---|---|---|---|---|")
        for r in rows[-15:]:
            md.append(
                f"| {r['opened_at'][:16]} | {r['kind']} | {_fmt(r.get('width'), '.0f')} "
                f"| {_fmt(r.get('credit_est'))}→{_fmt(r.get('credit_fill'))} "
                f"| {r.get('exit_reason') or '—'}{' (L)' if r.get('limit_exit') else ''} "
                f"| {_fmt(r.get('exit_cost_est'))}→{_fmt(r.get('exit_cost_fill'))} "
                f"| {_fmt(r.get('pnl'))} | {_fmt(r.get('gex_net'), '.2g')} |")
        md.append("")

    md.append("## Standing kill rule (adopted R11)\n")
    md.append("At n ≥ 500 live trades: if the bootstrapped 95% CI upper bound of $/trade "
              "is below $0, the strategy family is retired — hard-coded commitment, "
              "no appeals to 'one more tweak'.\n")
    md.append("---\n*Sources: `trades.db` (TradeLog) · protocol: "
              "[RESEARCH_PROTOCOL.md](../RESEARCH_PROTOCOL.md) · full research record: "
              "[AI_REVIEW.md](../AI_REVIEW.md) · system reference: [SYSTEM.md](../../SYSTEM.md)*")

    out = out_dir / "index.md"
    out.write_text("\n".join(md))
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Generate the dashboard")
    p.add_argument("--db", default="trades.db")
    p.add_argument("--out", default=str(OUT_DIR))
    a = p.parse_args()
    path = generate(a.db, Path(a.out))
    print(f"Dashboard written: {path}")


if __name__ == "__main__":
    main()
