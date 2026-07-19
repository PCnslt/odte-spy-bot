"""G2-FORWARD — zero-cost forward validation gate (docs/PREREGISTRATION_G2FWD.md).

Registered 2026-07-20 BEFORE any forward archive or paper-fill evidence existed; the constants
are pinned by tests/test_prereg_g2fwd.py. This gate ADDS to the plan — it never replaces or
relaxes G2 (nbbo_backtest.py), whose constants stay sealed and untouched. A PASS here
authorizes at most FWD_ALLOC_CAP_USD of live allocation; only the real G2 can authorize more.
"""
from __future__ import annotations

import random
from typing import Sequence

# --- pre-registered constants (DO NOT EDIT after data exists; pinned by tests) --------------
FWD_MIN_SESSIONS = 60
FWD_MIN_TRADES = 200
FWD_MIN_PF = 1.15                  # same bar as G2 — the gate is weaker in POWER, not in BAR
FWD_CI_LOWER_GT = 0.0
FWD_COST_QUANTILE = 0.90           # q90 of tradeable widths per bucket, never the mean
FWD_BASIS_MODE = "p90"             # delayed→real basis haircut at its 90th percentile
FWD_MIN_BASIS_N = 40               # fills needed before the basis estimate may be used
FWD_UNTRADEABLE_MAX = 0.20         # >20% one-sided/crossed => day excluded from capacity claims
FWD_BOOT_N = 10_000
FWD_BOOT_SEED = 20260720
FWD_ALLOC_CAP_USD = 100_000


def fwd_bootstrap_ci_lower(pnls: Sequence[float], alpha: float = 0.05,
                           n_boot: int = FWD_BOOT_N, seed: int = FWD_BOOT_SEED) -> float:
    """Deterministic bootstrap lower bound of mean $/trade (same construction as G2's)."""
    pnls = list(pnls)
    if not pnls:
        return float("-inf")
    rng = random.Random(seed)
    n = len(pnls)
    means = sorted(sum(rng.choice(pnls) for _ in range(n)) / n for _ in range(n_boot))
    return means[int(alpha * n_boot)]


def evaluate_g2_forward(pnls: Sequence[float], n_sessions: int, n_basis_fills: int) -> dict:
    """The registered G2-FORWARD verdict. Refuses to run before the evidence floor is met
    (that is NOT a FAIL — it is 'not yet runnable'); once runnable, PASS needs every leg."""
    if n_sessions < FWD_MIN_SESSIONS or len(pnls) < FWD_MIN_TRADES \
            or n_basis_fills < FWD_MIN_BASIS_N:
        return {"verdict": "NOT_RUNNABLE",
                "need": {"sessions": FWD_MIN_SESSIONS - n_sessions,
                         "trades": FWD_MIN_TRADES - len(pnls),
                         "basis_fills": FWD_MIN_BASIS_N - n_basis_fills}}
    gp = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    pf = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)
    ci = fwd_bootstrap_ci_lower(pnls)
    ok = pf >= FWD_MIN_PF and ci > FWD_CI_LOWER_GT
    return {"verdict": "PASS" if ok else "FAIL", "n_trades": len(pnls),
            "profit_factor": pf, "ci_lower_per_trade": round(ci, 4),
            "alloc_cap_usd": FWD_ALLOC_CAP_USD if ok else 0}
