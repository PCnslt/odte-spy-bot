"""Pre-registered NBBO backtest harness — Master Plan v2, gate G2.

The constants below were PRE-REGISTERED on 2026-07-19 (docs/PREREGISTRATION_V2.md) before any
NBBO data was purchased or seen; tests/test_nbbo_backtest.py pins their exact values so an
accidental edit fails the suite. Changing them after results exist is a protocol violation —
the gate's entire value is that a FAIL cannot be negotiated with.

Fill model is deliberately CONSERVATIVE: SEC DERA (2025) measures patient 0DTE mid-point limit
orders costing ~half of crossing (fill rate 50–62%), while this model ALWAYS crosses 75% of the
quoted width single-leg / 53% multi-leg (ORATS). A PASS here understates live execution quality.

No data fabrication: load_nbbo() fails closed until a real ThetaData subscription exists.
"""
from __future__ import annotations

import math
import random
from typing import Optional, Sequence

# --- G2 pre-registered constants (DO NOT EDIT after data is seen; pinned by tests) ----------
SAMPLE_START = "2022-05-16"        # first day of the all-weekday 0DTE era
PASS_MIN_TRADES = 350
PASS_MIN_PF = 1.15
COMMISSION_PER_CONTRACT = 0.65     # IBKR fixed, all-in
ORDER_MIN_PER_LEG = 1.00
CROSS_FRAC_SINGLE = 0.75           # ORATS: fills cross 75% of quoted width (single leg)
CROSS_FRAC_MULTI = 0.53            # ...53% for multi-leg combos
EVENT_WIDTH_MULT = 2.0             # width multiplier first/last 15 min + event days
BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 20260719          # deterministic — same inputs, same verdict, forever


# --- fill model -----------------------------------------------------------------------------
def effective_fill(side: int, bid: float, ask: float, n_legs: int = 1,
                   widen: float = 1.0) -> Optional[float]:
    """Effective fill price: cross CROSS_FRAC of the (possibly widened) quoted width from mid.

    side: +1 = buy (pay up), -1 = sell (give down). `widen` models stressed books (first/last
    15 minutes, event days) — fills may land beyond the displayed touch by design. Returns None
    on an unusable quote (missing/crossed/non-positive), so callers must skip, never guess."""
    if side not in (1, -1):
        raise ValueError("side must be +1 (buy) or -1 (sell)")
    if bid is None or ask is None or math.isnan(bid) or math.isnan(ask):
        return None
    if bid < 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    frac = CROSS_FRAC_MULTI if n_legs > 1 else CROSS_FRAC_SINGLE
    return mid + side * frac * ((ask - bid) * widen) / 2.0


# --- cost model -----------------------------------------------------------------------------
def leg_commission(qty: int) -> float:
    """IBKR fixed schedule for one leg-order: $0.65/contract with a $1.00 per-order minimum."""
    if qty <= 0:
        return 0.0
    return max(ORDER_MIN_PER_LEG, qty * COMMISSION_PER_CONTRACT)


def round_trip_commission(leg_qtys: Sequence[int]) -> float:
    """Open + close commissions across all legs (per-leg order minimums apply to combos)."""
    return 2.0 * sum(leg_commission(q) for q in leg_qtys)


# --- metrics --------------------------------------------------------------------------------
def profit_factor(pnls: Sequence[float]) -> float:
    """Gross profit / gross loss. inf when there are no losing trades (evaluate still
    requires the CI and sample-size legs, so an inf PF alone can never produce a PASS)."""
    gp = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    if gl == 0:
        return float("inf") if gp > 0 else 0.0
    return gp / gl


def bootstrap_ci_lower(pnls: Sequence[float], alpha: float = 0.05,
                       n_boot: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED) -> float:
    """Deterministic bootstrap lower bound of mean $/trade. Same inputs -> same number,
    always — a verdict that changes on re-run is not a verdict."""
    pnls = list(pnls)
    if not pnls:
        return float("-inf")
    rng = random.Random(seed)
    n = len(pnls)
    means = sorted(sum(rng.choice(pnls) for _ in range(n)) / n for _ in range(n_boot))
    return means[int(alpha * n_boot)]


def evaluate_g2(pnls: Sequence[float]) -> dict:
    """The pre-registered G2 verdict. PASS requires ALL legs; anything else is FAIL."""
    pnls = list(pnls)
    n = len(pnls)
    pf = profit_factor(pnls)
    ci_lower = bootstrap_ci_lower(pnls)
    verdict = "PASS" if (n >= PASS_MIN_TRADES and pf >= PASS_MIN_PF and ci_lower > 0.0) \
        else "FAIL"
    return {"verdict": verdict, "n_trades": n, "profit_factor": round(pf, 4)
            if pf != float("inf") else pf, "ci_lower_per_trade": round(ci_lower, 4),
            "criteria": {"min_trades": PASS_MIN_TRADES, "min_pf": PASS_MIN_PF,
                         "ci_lower_gt": 0.0, "sample_start": SAMPLE_START}}


# --- data boundary (fail-closed) ------------------------------------------------------------
def load_nbbo(*_args, **_kwargs):
    """Gate G2 requires real ThetaData Options Standard NBBO quotes. Until that subscription
    exists this harness refuses to run — it never substitutes trades, marks, or synthetic
    quotes for the bid/ask (the exact corner every discredited 0DTE backtest cuts)."""
    raise RuntimeError(
        "NBBO data unavailable: ThetaData Options Standard is not subscribed. "
        "G2 cannot run on fabricated or trade-price data — see docs/PREREGISTRATION_V2.md.")
