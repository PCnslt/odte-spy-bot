"""TradeLog: the system's memory of every live trade WITH its full decision context.

Why this exists: four proposed upgrades (IV/RV gating, fill-quality prediction, dynamic
profit targets, regime clustering) all failed the same test — no training data. This table
IS that training data. Every paper trade records what the system believed at entry (regime,
ML prob, range forecast, breach probs, IV, RV) and what actually happened (fills vs quotes,
slippage, exit path). When enough rows exist, the deferred ideas become testable against
reality instead of a reused 90-day backtest window.

SQLite, same zero-ops philosophy as memory.py.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .logger import get_logger

log = get_logger("trade_log")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    kind TEXT NOT NULL,
    short_strike REAL, long_strike REAL, width REAL, quantity INTEGER,
    -- entry pricing
    credit_est REAL,          -- estimated credit used for the decision
    credit_fill REAL,         -- actual filled credit
    entry_slippage REAL,      -- credit_est - credit_fill (positive = worse than expected)
    -- exit pricing
    exit_reason TEXT,
    exit_cost_est REAL,       -- cost estimate when the close was triggered
    exit_cost_fill REAL,      -- actual filled close cost
    exit_slippage REAL,       -- exit_cost_fill - exit_cost_est (positive = worse)
    pnl REAL,
    -- decision context at entry
    spot REAL, regime TEXT, ml_prob REAL, range_pred REAL,
    p_breach_dn REAL, p_breach_up REAL,
    iv_short REAL,            -- implied vol of the short leg (Polygon snapshot), if available
    rv_annual REAL,           -- realized vol (annualized, 5-min window) from live features
    rv_60m REAL,              -- realized vol (annualized) over the trailing 60 minutes (H1)
    rvol REAL, atr_5 REAL, minutes_into_session REAL,
    limit_exit INTEGER        -- 1 if closed via limit order, 0 if market
);
"""


class TradeLog:
    # Columns added after the original schema: migrated in-place (SQLite ADD COLUMN).
    _MIGRATIONS = ["rv_60m REAL",
                   # audit m1: the OTHER width arm's credit estimate at the same entry —
                   # lets H2b condition on "both arms would have passed min_credit".
                   "alt_width_credit_est REAL",
                   # R10 / H7: naive 0DTE gamma exposure at session start (telemetry).
                   "gex_net REAL", "gamma_wall REAL",
                   # R12 / H8-H9: market-implied risk features from the chain snapshot.
                   "short_delta REAL",   # short leg's delta at entry
                   "prob_touch REAL",    # 2*|delta| — market-implied P(reach short strike)
                   "iv_atm REAL",        # ATM IV (session)
                   "skew_25d REAL"]      # 25-delta risk reversal (session)

    def __init__(self, db_path: str | Path = "trades.db"):
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        for col in self._MIGRATIONS:
            try:
                self._conn.execute(f"ALTER TABLE trades ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # already present
        self._conn.commit()

    def open_trade(self, **kw) -> int:
        cols = ["opened_at", "kind", "short_strike", "long_strike", "width", "quantity",
                "credit_est", "alt_width_credit_est", "spot", "regime", "ml_prob",
                "range_pred", "p_breach_dn", "p_breach_up", "iv_short", "rv_annual",
                "rv_60m", "rvol", "atr_5", "minutes_into_session", "gex_net", "gamma_wall",
                "short_delta", "prob_touch", "iv_atm", "skew_25d"]
        vals = [kw.get(c) for c in cols]
        cur = self._conn.execute(
            f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals)
        self._conn.commit()
        return int(cur.lastrowid)

    def close_trade(self, trade_id: int, *, closed_at: str, exit_reason: str,
                    exit_cost_est: Optional[float], exit_cost_fill: Optional[float],
                    credit_fill: Optional[float], pnl: float, limit_exit: bool) -> None:
        entry_slip = None
        row = self._conn.execute("SELECT credit_est FROM trades WHERE id=?",
                                 (trade_id,)).fetchone()
        if row and row["credit_est"] is not None and credit_fill is not None:
            entry_slip = round(row["credit_est"] - credit_fill, 4)
        exit_slip = None
        if exit_cost_est is not None and exit_cost_fill is not None:
            exit_slip = round(exit_cost_fill - exit_cost_est, 4)
        self._conn.execute(
            "UPDATE trades SET closed_at=?, exit_reason=?, exit_cost_est=?,"
            " exit_cost_fill=?, credit_fill=?, entry_slippage=?, exit_slippage=?, pnl=?,"
            " limit_exit=? WHERE id=?",
            (closed_at, exit_reason, exit_cost_est, exit_cost_fill, credit_fill,
             entry_slip, exit_slip, pnl, 1 if limit_exit else 0, trade_id))
        self._conn.commit()

    # --- analysis ---------------------------------------------------------------
    def count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) c FROM trades WHERE closed_at IS NOT NULL").fetchone()["c"])

    def report(self) -> str:
        """Per-regime / slippage / IV-vs-RV summary. Honest about sample size."""
        n = self.count()
        lines = [f"TradeLog: {n} closed trades"]
        if n == 0:
            return lines[0]
        if n < 30:
            lines.append(f"(n={n} < 30 — everything below is noise; do not act on it)")
        for row in self._conn.execute(
                "SELECT regime, COUNT(*) n, AVG(pnl) avg_pnl,"
                " AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) win"
                " FROM trades WHERE closed_at IS NOT NULL GROUP BY regime"):
            lines.append(f"  regime={row['regime'] or '?':10s} n={row['n']:4d} "
                         f"win={row['win']:.0%} avg_pnl=${row['avg_pnl']:.2f}")
        slip = self._conn.execute(
            "SELECT AVG(entry_slippage) es, AVG(exit_slippage) xs,"
            " AVG(CASE WHEN limit_exit=1 THEN exit_slippage END) xs_limit,"
            " AVG(CASE WHEN limit_exit=0 THEN exit_slippage END) xs_market"
            " FROM trades WHERE closed_at IS NOT NULL").fetchone()
        lines.append(f"  slippage: entry={slip['es'] if slip['es'] is not None else 'n/a'} "
                     f"exit(limit)={slip['xs_limit']} exit(market)={slip['xs_market']}")
        ivrv = self._conn.execute(
            "SELECT AVG(pnl) p, COUNT(*) n FROM trades WHERE closed_at IS NOT NULL"
            " AND iv_short IS NOT NULL AND rv_annual IS NOT NULL AND iv_short > rv_annual"
        ).fetchone()
        if ivrv["n"]:
            lines.append(f"  IV>RV entries: n={ivrv['n']} avg_pnl=${ivrv['p']:.2f}")
        return "\n".join(lines)

    def close(self) -> None:
        self._conn.close()


def _main() -> None:  # python -m src.utils.trade_log
    import argparse

    p = argparse.ArgumentParser(description="Trade-log report")
    p.add_argument("--db", default="trades.db")
    args = p.parse_args()
    tl = TradeLog(args.db)
    print(tl.report())
    tl.close()


if __name__ == "__main__":
    _main()
