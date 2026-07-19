"""Tamper seal for G2-FORWARD (docs/PREREGISTRATION_G2FWD.md, registered 2026-07-20).

Same contract as tests/test_nbbo_backtest.py: these constants were fixed BEFORE any forward
archive or paper-fill evidence existed. If this test fails, someone edited a registered
criterion after the fact — that is a protocol violation, not a test to 'fix'.
"""
from __future__ import annotations

from src.research import g2_forward as g


def test_g2fwd_constants_are_sealed():
    assert g.FWD_MIN_SESSIONS == 60
    assert g.FWD_MIN_TRADES == 200
    assert g.FWD_MIN_PF == 1.15
    assert g.FWD_CI_LOWER_GT == 0.0
    assert g.FWD_COST_QUANTILE == 0.90
    assert g.FWD_BASIS_MODE == "p90"
    assert g.FWD_MIN_BASIS_N == 40
    assert g.FWD_UNTRADEABLE_MAX == 0.20
    assert g.FWD_BOOT_N == 10_000
    assert g.FWD_BOOT_SEED == 20260720
    assert g.FWD_ALLOC_CAP_USD == 100_000


def test_g2fwd_refuses_to_run_early():
    r = g.evaluate_g2_forward([1.0] * 10, n_sessions=5, n_basis_fills=3)
    assert r["verdict"] == "NOT_RUNNABLE"
    assert r["need"]["trades"] == 190 and r["need"]["sessions"] == 55


def test_g2fwd_pass_requires_every_leg():
    wins = [10.0] * 150 + [-5.0] * 50                 # PF = 1500/250 = 6, CI > 0
    r = g.evaluate_g2_forward(wins, n_sessions=60, n_basis_fills=40)
    assert r["verdict"] == "PASS" and r["alloc_cap_usd"] == 100_000
    flat = [1.0, -1.0] * 100                          # PF = 1.0 < 1.15
    r2 = g.evaluate_g2_forward(flat, n_sessions=60, n_basis_fills=40)
    assert r2["verdict"] == "FAIL" and r2["alloc_cap_usd"] == 0


def test_g2fwd_verdict_is_deterministic():
    pnls = [3.0, -1.0, 2.0, -2.0] * 60
    a = g.evaluate_g2_forward(pnls, 60, 40)
    b = g.evaluate_g2_forward(pnls, 60, 40)
    assert a == b


def test_g2_real_constants_untouched():
    """G2-FORWARD must never modify the real G2 seal — spot-check the original pins."""
    from src.research import nbbo_backtest as n
    assert n.PASS_MIN_TRADES == 350 and n.PASS_MIN_PF == 1.15
    assert n.CROSS_FRAC_SINGLE == 0.75 and n.CROSS_FRAC_MULTI == 0.53
    assert n.BOOTSTRAP_SEED == 20260719
