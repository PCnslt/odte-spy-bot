"""Settlement math for the registered 1×2-put-ratio-plus-wing structure (XSP).

Pure functions, test-sealed. Exists because the external advisor proposed a "zero-floor
butterfly collected as a $4.00 NET CREDIT with L_max = $0" — a no-arbitrage violation (a
non-negative payoff must trade at a debit). Any parameterization claiming credit > 0 with
L_max <= 0 is machine-rejected here so a free-lunch spec can never reach the backtest.
"""
from __future__ import annotations

MULT = 100.0   # XSP multiplier


def settlement_payoff_pts(s: float, k_long: float, k_short: float, k_wing: float) -> float:
    """Expiry payoff in index points of +1 put @k_long, -2 puts @k_short, +1 put @k_wing."""
    p = max(k_long - s, 0.0) - 2.0 * max(k_short - s, 0.0) + max(k_wing - s, 0.0)
    return p


def ratio_wing_l_max(k_long: float, k_short: float, k_wing: float,
                     net_credit_usd: float) -> float:
    """Hedge-inclusive worst-case settlement loss in dollars per unit (Amendment 3C).

    Requires k_wing < k_short < k_long. The payoff floor below the wing is
    (k_long + k_wing - 2*k_short) points; the minimum payoff overall is min(0, floor),
    reached at/below the wing. L_max = max(0, -(credit + min_payoff*100))."""
    if not (k_wing < k_short < k_long):
        raise ValueError("need k_wing < k_short < k_long for a put ratio + lower wing")
    floor_pts = k_long + k_wing - 2.0 * k_short
    worst_pnl = net_credit_usd + min(0.0, floor_pts) * MULT
    return max(0.0, -worst_pnl)


def violates_no_free_lunch(net_credit_usd: float, l_max_usd: float) -> bool:
    """True for the impossible claim 'collect a credit with zero (or negative) worst case'.
    No-arbitrage: a position that can never lose and pays you to enter does not exist at
    fair quotes; such a spec is a pricing/arithmetic error, never a strategy."""
    return net_credit_usd > 0.0 and l_max_usd <= 0.0


def fundable(l_max_usd: float, net_credit_usd: float, tail_budget_usd: float,
             min_credit_usd: float = 10.0) -> bool:
    """Amendment 3C funding rule: one unit fits the daily tail budget, credit clears the
    floor, and the spec is not a free-lunch arithmetic error."""
    return (not violates_no_free_lunch(net_credit_usd, l_max_usd)
            and 0.0 < l_max_usd <= tail_budget_usd
            and net_credit_usd >= min_credit_usd)
