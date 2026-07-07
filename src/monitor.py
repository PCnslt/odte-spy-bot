"""Strategy-death-spiral monitor — the early-warning half of the kill rule.

Pure analysis over trades.db (the TradeLog). Touches NO live-trading logic, so it can only
observe, never mis-fire an order. It answers one question the operator must see every day:
*is the accumulating live evidence already telling us this strategy family is dead, before we
reach the hard 500-trade kill rule?*

Mechanism (matches RESEARCH_PROTOCOL.md §Standing rules 0):
  - Bootstrap the 95% CI of $/trade from all closed trades.
  - HARD kill rule: at n >= 500, if the CI UPPER bound < $0 -> RETIRE.
  - EARLY warning: at n >= 100, if the CI upper bound is already < $0 -> KILL_WATCH (a soft
    pre-trigger; the operator should consider stopping without waiting for 500).
Secondary health signals (cost is the alpha): mean entry+exit slippage, and the count of
trailing consecutive losing SESSIONS (grouped by ET calendar date).

    python -m src.monitor --db trades.db
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np

# Thresholds (documented, not magic): CI needs a floor of trades to mean anything; the early
# watch fires at 100, the hard kill rule at 500 (protocol). Seed is fixed so the flag a human
# sees is reproducible from the same trades.db — no run-to-run flicker on a boundary case.
MIN_N_CI = 30
WATCH_N = 100
KILL_N = 500
BOOTSTRAP_ITERS = 10_000
SEED = 12345


def bootstrap_ci(pnls, iters: int = BOOTSTRAP_ITERS, alpha: float = 0.05,
                 seed: int = SEED) -> tuple[float, float, float]:
    """(ci_lo, mean, ci_hi) of the MEAN $/trade via a seeded percentile bootstrap.

    Deterministic for a given (pnls, iters, alpha, seed) so the dashboard flag is stable.
    n<2 -> the CI collapses to the point estimate (nothing to resample)."""
    arr = np.asarray(list(pnls), dtype=float)
    n = len(arr)
    if n == 0:
        return (0.0, 0.0, 0.0)
    mean = float(arr.mean())
    if n < 2:
        return (mean, mean, mean)
    rng = np.random.default_rng(seed)
    # Vectorized: iters x n index matrix -> row means.
    idx = rng.integers(0, n, size=(iters, n))
    means = arr[idx].mean(axis=1)
    lo = float(np.quantile(means, alpha / 2))
    hi = float(np.quantile(means, 1 - alpha / 2))
    return (lo, mean, hi)


def _closed_rows(db_path: str) -> list[dict]:
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT opened_at, pnl, entry_slippage, exit_slippage "
            "FROM trades WHERE closed_at IS NOT NULL AND pnl IS NOT NULL "
            "ORDER BY opened_at")]
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return rows


def _consecutive_losing_sessions(rows: list[dict]) -> int:
    """Trailing count of ET calendar dates whose summed P&L is <= 0."""
    by_day: dict[str, float] = {}
    for r in rows:
        day = (r["opened_at"] or "")[:10]
        by_day[day] = by_day.get(day, 0.0) + (r["pnl"] or 0.0)
    streak = 0
    for day in sorted(by_day)[::-1]:
        if by_day[day] <= 0:
            streak += 1
        else:
            break
    return streak


def death_spiral_check(db_path: str = "trades.db") -> dict:
    """Read closed trades and return the health verdict (see module docstring for flags)."""
    rows = _closed_rows(db_path)
    n = len(rows)
    pnls = [r["pnl"] or 0.0 for r in rows]
    lo, mean, hi = bootstrap_ci(pnls)
    slips = [(r["entry_slippage"] or 0.0) + (r["exit_slippage"] or 0.0)
             for r in rows if r["entry_slippage"] is not None or r["exit_slippage"] is not None]
    mean_slip = float(np.mean(slips)) if slips else None
    consec = _consecutive_losing_sessions(rows)

    if n < MIN_N_CI:
        flag = "INSUFFICIENT"
        reason = f"n={n} < {MIN_N_CI}: too few closed trades to judge; keep accumulating."
    elif n >= KILL_N and hi < 0:
        flag = "RETIRE"
        reason = (f"KILL RULE: n={n} >= {KILL_N} and 95% CI upper (${hi:.2f}) < $0 — "
                  "retire the strategy family per protocol.")
    elif n >= WATCH_N and hi < 0:
        flag = "KILL_WATCH"
        reason = (f"EARLY WARNING: n={n} >= {WATCH_N} and 95% CI upper (${hi:.2f}) already "
                  "< $0 — evidence is trending to a kill before n=500. Consider stopping.")
    else:
        flag = "HEALTHY"
        reason = (f"CI upper (${hi:.2f}) not yet below $0 at n={n} — no death-spiral signal."
                  if hi >= 0 else f"n={n} < {WATCH_N}: below the early-warning floor.")

    return {"n": n, "mean": mean, "ci_lo": lo, "ci_hi": hi,
            "consec_losing_sessions": consec, "mean_slippage": mean_slip,
            "flag": flag, "reason": reason}


def panel_markdown(state: dict) -> list[str]:
    """Render the death-spiral state as dashboard markdown lines."""
    badge = {"HEALTHY": "🟢 HEALTHY", "INSUFFICIENT": "⚪ INSUFFICIENT DATA",
             "KILL_WATCH": "🟠 KILL-WATCH (early warning)", "RETIRE": "🔴 RETIRE (kill rule)"}
    md = ["## Early-warning — strategy death-spiral monitor\n",
          f"**{badge.get(state['flag'], state['flag'])}** · {state['reason']}\n",
          "| Closed trades | $/trade | 95% CI | Consec. losing sessions | Mean slippage |",
          "|---|---|---|---|---|"]
    slip = "—" if state["mean_slippage"] is None else f"${state['mean_slippage']:.3f}"
    md.append(f"| {state['n']} | ${state['mean']:.2f} | "
              f"[${state['ci_lo']:.2f}, ${state['ci_hi']:.2f}] | "
              f"{state['consec_losing_sessions']} | {slip} |\n")
    return md


def main() -> None:
    p = argparse.ArgumentParser(description="Strategy death-spiral monitor")
    p.add_argument("--db", default="trades.db")
    args = p.parse_args()
    state = death_spiral_check(args.db)
    print("\n".join(panel_markdown(state)))
    # Exit non-zero on a kill/watch so schedulers can alert on it.
    raise SystemExit(2 if state["flag"] in ("KILL_WATCH", "RETIRE") else 0)


if __name__ == "__main__":
    main()
