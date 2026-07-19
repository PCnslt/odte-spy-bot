"""G2 harness tests. The constants test is a TAMPER SEAL: it pins the pre-registered values
(docs/PREREGISTRATION_V2.md, 2026-07-19). If a future edit changes any of them, this fails —
by design. Do not update the expected values here without updating the pre-registration
rationale, and never after G2 data has been seen."""
from __future__ import annotations

import pytest

from src.research import nbbo_backtest as nb


# --- tamper seal ----------------------------------------------------------------------------
def test_preregistered_constants_are_sealed():
    assert nb.SAMPLE_START == "2022-05-16"
    assert nb.PASS_MIN_TRADES == 350
    assert nb.PASS_MIN_PF == 1.15
    assert nb.COMMISSION_PER_CONTRACT == 0.65
    assert nb.ORDER_MIN_PER_LEG == 1.00
    assert nb.CROSS_FRAC_SINGLE == 0.75
    assert nb.CROSS_FRAC_MULTI == 0.53
    assert nb.EVENT_WIDTH_MULT == 2.0
    assert nb.BOOTSTRAP_SEED == 20260719


# --- fill model -----------------------------------------------------------------------------
def test_fill_buy_pays_up_sell_gives_down():
    buy = nb.effective_fill(+1, 1.00, 1.10)
    sell = nb.effective_fill(-1, 1.00, 1.10)
    mid = 1.05
    assert buy > mid > sell
    assert buy == pytest.approx(mid + 0.75 * 0.10 / 2)
    assert sell == pytest.approx(mid - 0.75 * 0.10 / 2)


def test_multi_leg_crosses_less_than_single():
    single = nb.effective_fill(+1, 1.00, 1.10, n_legs=1)
    multi = nb.effective_fill(+1, 1.00, 1.10, n_legs=3)
    assert multi < single                      # 53% vs 75% of the width


def test_widen_makes_fills_worse_and_can_pass_the_touch():
    calm = nb.effective_fill(+1, 1.00, 1.10, widen=1.0)
    stressed = nb.effective_fill(+1, 1.00, 1.10, widen=nb.EVENT_WIDTH_MULT)
    assert stressed > calm
    assert stressed > 1.10                     # beyond the displayed ask, by design


def test_bad_quotes_return_none_never_a_guess():
    assert nb.effective_fill(+1, None, 1.10) is None
    assert nb.effective_fill(+1, 1.20, 1.10) is None     # crossed
    assert nb.effective_fill(+1, -0.05, 0.05) is None
    assert nb.effective_fill(+1, float("nan"), 1.0) is None
    with pytest.raises(ValueError):
        nb.effective_fill(0, 1.0, 1.1)


# --- cost model -----------------------------------------------------------------------------
def test_order_minimum_binds_small_legs():
    assert nb.leg_commission(1) == 1.00        # $0.65 < $1 minimum
    assert nb.leg_commission(4) == pytest.approx(2.60)


def test_round_trip_covers_all_legs_open_and_close():
    # put ratio: 1 long + 2 short contracts -> legs [1, 2]
    assert nb.round_trip_commission([1, 2]) == pytest.approx(2 * (1.00 + 1.30))
    # 4-lot vertical: [4, 4] -> 2 * (2.60 + 2.60) = $10.40
    assert nb.round_trip_commission([4, 4]) == pytest.approx(10.40)


# --- metrics --------------------------------------------------------------------------------
def test_profit_factor_basics():
    assert nb.profit_factor([10, -5]) == pytest.approx(2.0)
    assert nb.profit_factor([1, 1]) == float("inf")
    assert nb.profit_factor([-1]) == 0.0


def test_bootstrap_is_deterministic_and_sane():
    pnls = [5.0] * 50 + [-3.0] * 30
    a = nb.bootstrap_ci_lower(pnls)
    b = nb.bootstrap_ci_lower(pnls)
    assert a == b                              # same seed, same verdict, forever
    mean = sum(pnls) / len(pnls)
    assert a < mean                            # lower bound sits below the mean
    assert nb.bootstrap_ci_lower([1.0] * 20) == pytest.approx(1.0)


# --- the gate itself ------------------------------------------------------------------------
def test_g2_fails_on_sample_size_alone():
    r = nb.evaluate_g2([2.0] * 100)            # great pnls, too few trades
    assert r["verdict"] == "FAIL" and r["n_trades"] == 100


def test_g2_fails_on_weak_profit_factor():
    pnls = ([1.0] * 200 + [-1.0] * 200)        # PF 1.0 < 1.15
    assert nb.evaluate_g2(pnls)["verdict"] == "FAIL"


def test_g2_pass_requires_all_three_legs():
    # 400 trades, PF well above 1.15, CI-lower > 0
    pnls = [30.0] * 260 + [-20.0] * 140
    r = nb.evaluate_g2(pnls)
    assert r["verdict"] == "PASS"
    assert r["n_trades"] == 400
    assert r["profit_factor"] > nb.PASS_MIN_PF
    assert r["ci_lower_per_trade"] > 0


def test_data_boundary_fails_closed():
    with pytest.raises(RuntimeError, match="ThetaData"):
        nb.load_nbbo("XSP", "2022-05-16")
