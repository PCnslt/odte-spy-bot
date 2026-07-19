"""Seals the no-free-lunch guard and the hedge-inclusive L_max math (Prereg Amendment 4)."""
from __future__ import annotations

import pytest

from src.strategy.structure_math import (fundable, ratio_wing_l_max, settlement_payoff_pts,
                                         violates_no_free_lunch)


def test_deepseek_zero_floor_credit_butterfly_is_rejected():
    """The advisor's exact spec: 743/740/737 'collected as a $4.00 credit', L_max claimed $0.
    The zero floor is real; the CREDIT is the arbitrage error. Machine-rejected."""
    l_max = ratio_wing_l_max(743.0, 740.0, 737.0, net_credit_usd=400.0)
    assert l_max == 0.0                                    # floor genuinely zero...
    assert violates_no_free_lunch(400.0, l_max) is True    # ...so a credit is impossible
    assert fundable(l_max, 400.0, tail_budget_usd=500.0) is False


def test_realistic_credit_ratio_with_wing_is_fundable():
    """A real credit triple (long 743, shorts 742, wing 737): floor -4 pts, credit ~$35 ->
    L_max = 400-35 = $365 <= $500 budget, credit >= $10. Fundable at 0.5% x $100k."""
    l_max = ratio_wing_l_max(743.0, 742.0, 737.0, net_credit_usd=35.0)
    assert l_max == pytest.approx(365.0)
    assert violates_no_free_lunch(35.0, l_max) is False
    assert fundable(l_max, 35.0, tail_budget_usd=500.0) is True


def test_l_max_exceeding_budget_is_not_fundable():
    l_max = ratio_wing_l_max(743.0, 740.0, 730.0, net_credit_usd=25.0)   # floor -7 pts
    assert l_max == pytest.approx(675.0)
    assert fundable(l_max, 25.0, tail_budget_usd=500.0) is False


def test_payoff_shape_is_correct():
    # long fly region checks for 743/740/737
    assert settlement_payoff_pts(750, 743, 740, 737) == 0.0
    assert settlement_payoff_pts(740, 743, 740, 737) == pytest.approx(3.0)   # peak at body
    assert settlement_payoff_pts(737, 743, 740, 737) == pytest.approx(0.0)
    assert settlement_payoff_pts(700, 743, 740, 737) == pytest.approx(0.0)   # flat below wing
    # true 1x2-with-lower-wing: negative floor below the wing
    assert settlement_payoff_pts(700, 743, 742, 737) == pytest.approx(-4.0)


def test_strike_ordering_enforced():
    with pytest.raises(ValueError):
        ratio_wing_l_max(743.0, 740.0, 741.0, 10.0)        # wing above short = degenerate
