"""Operator briefing — one plain-English line that tells a non-quant what to do.

The whole system reduces to ONE question: are real fills beating the pessimistic cost model?
If yes, this strategy might live; if no, it should die. This module turns the numbers in
trades.db into a "bottom line" a tired human can read in three seconds:

    BOTTOM LINE: <emoji> <headline>
    <what the fills are doing vs the pessimistic model — the whole game>
    WHAT TO DO: <one instruction>

Pure read-over-trades.db. No trading impact. Rendered at the top of the dashboard and printed
at end of day.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from .monitor import death_spiral_check

# Total (entry+exit) per-trade slippage bands, in $/share. Near mid = the OPTIMISTIC world in
# which this strategy has a chance; wide = the PESSIMISTIC world in which it's dead.
FILL_GOOD = 0.10   # <= this: fills near mid (good case)
FILL_BAD = 0.30    # >= this: fills tracking the pessimistic model (bad case)


def _stats(db_path: str) -> dict:
    if not Path(db_path).exists():
        return {"n": 0, "net": 0.0, "slip": None}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT pnl, entry_slippage, exit_slippage FROM trades "
            "WHERE closed_at IS NOT NULL "
            "AND IFNULL(exit_reason,'') != 'reconciled_unfilled'")]   # not a real fill
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    n = len(rows)
    net = sum((r["pnl"] or 0.0) for r in rows)
    slips = [(r["entry_slippage"] or 0.0) + (r["exit_slippage"] or 0.0)
             for r in rows if r["entry_slippage"] is not None or r["exit_slippage"] is not None]
    slip = (sum(slips) / len(slips)) if slips else None
    return {"n": n, "net": net, "slip": slip}


def _fill_verdict(slip: float | None) -> str:
    if slip is None:
        return "No fills yet — the make-or-break number (real fills vs the pessimistic model) is still blank."
    if slip <= FILL_GOOD:
        return (f"Fills are landing near mid (~${slip:.2f}/trade of slippage) — the GOOD case; "
                "real fills are beating the pessimistic model. This is the only path to survival.")
    if slip >= FILL_BAD:
        return (f"Fills are expensive (~${slip:.2f}/trade of slippage) — tracking the PESSIMISTIC "
                "model. This is the case where the strategy is dead.")
    return (f"Fills (~${slip:.2f}/trade of slippage) sit between the optimistic and pessimistic "
            "models — still undecided.")


def briefing(db_path: str = "trades.db") -> dict:
    st = _stats(db_path)
    ds = death_spiral_check(db_path)
    n, net, slip = st["n"], st["net"], st["slip"]

    if n == 0:
        emoji, headline = "🟢", "Armed and healthy — it hasn't traded yet. Nothing for you to do."
        action = "Nothing. Let it run; check back after it has some trades."
    elif ds["flag"] == "RETIRE":
        emoji, headline = "🔴", "STOP — the kill rule fired. The evidence says this strategy is done."
        action = "Stop the bot and pivot. It has proven itself unprofitable at n≥500."
    elif ds["flag"] == "KILL_WATCH":
        emoji, headline = "🟠", "WATCH — the early-warning fired before n=500. It's likely dying."
        action = "Strongly consider stopping now, or pivot the machinery to a cheaper-cost strategy."
    elif slip is not None and slip >= FILL_BAD and n >= 20:
        emoji, headline = "🟠", "WATCH — real fills look expensive, tracking the pessimistic model."
        action = "Keep watching; if it holds through ~100 trades, plan to stop."
    else:
        emoji, headline = "🟢", "On track — no death-spiral signal, fills not (yet) confirming the bad case."
        action = "Nothing. Let the evidence accumulate; the bot kills itself early if it turns."

    lines = [f"BOTTOM LINE: {emoji} {headline}",
             _fill_verdict(slip),
             (f"So far: {n} trades · net ${net:.2f} · "
              f"kill-watch: {ds['flag'].replace('_', '-').lower()}."),
             f"WHAT TO DO: {action}"]
    return {"emoji": emoji, "headline": headline, "action": action,
            "n": n, "net": net, "slip": slip, "flag": ds["flag"], "text": "\n".join(lines)}


def panel_markdown(b: dict) -> list[str]:
    """Dashboard 'Bottom line' block — the first thing the operator sees."""
    return ["## Bottom line — plain English\n",
            f"### {b['emoji']} {b['headline']}\n",
            f"{_fill_verdict(b['slip'])}\n",
            f"*So far: {b['n']} trades · net ${b['net']:.2f} · "
            f"kill-watch: {b['flag'].replace('_', '-').lower()}.*\n",
            f"**What to do:** {b['action']}\n"]


def main() -> None:
    p = argparse.ArgumentParser(description="Plain-English operator briefing")
    p.add_argument("--db", default="trades.db")
    args = p.parse_args()
    print(briefing(args.db)["text"])


if __name__ == "__main__":
    main()
