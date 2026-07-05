"""Black-Scholes pricing + greeks for the synthetic 0DTE option used in sim/backtest.

IMPORTANT: This is a MODEL, not a market. Real 0DTE options have wide, jumpy spreads and
pin/gamma effects near the strike that Black-Scholes does not capture. Prices here are a
reasonable approximation for research, and deliberately pessimistic slippage is added on top
in the broker/backtester. Do not mistake modeled fills for what IBKR would actually give you.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..common import OptionRight

# 0DTE has essentially no time value from rates/divs; keep r small and explicit.
RISK_FREE_RATE = 0.05
TRADING_MINUTES_PER_YEAR = 252 * 390  # 252 sessions * 390 minutes


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float   # per-minute theta (premium decay per minute), negative for long
    vega: float


def years_to_expiry(minutes_left: float) -> float:
    """Convert minutes-to-close into an annualized fraction, floored to avoid div-by-zero."""
    return max(minutes_left, 0.5) / TRADING_MINUTES_PER_YEAR


def black_scholes(spot: float, strike: float, minutes_left: float, iv: float,
                  right: OptionRight, r: float = RISK_FREE_RATE) -> Greeks:
    """Price a European option + greeks. `iv` is annualized. Robust to tiny time-to-expiry."""
    t = years_to_expiry(minutes_left)
    iv = max(iv, 1e-4)
    sqrt_t = math.sqrt(t)

    if spot <= 0 or strike <= 0:
        return Greeks(0.0, 0.0, 0.0, 0.0, 0.0)

    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    disc = math.exp(-r * t)

    if right == OptionRight.CALL:
        price = spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
        delta = _norm_cdf(d1)
        theta_annual = (-(spot * _norm_pdf(d1) * iv) / (2 * sqrt_t)
                        - r * strike * disc * _norm_cdf(d2))
    else:
        price = strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1.0
        theta_annual = (-(spot * _norm_pdf(d1) * iv) / (2 * sqrt_t)
                        + r * strike * disc * _norm_cdf(-d2))

    gamma = _norm_pdf(d1) / (spot * iv * sqrt_t)
    vega = spot * _norm_pdf(d1) * sqrt_t          # per 1.00 (100%) vol move
    theta_per_minute = theta_annual / TRADING_MINUTES_PER_YEAR

    return Greeks(
        price=max(price, 0.0),
        delta=delta,
        gamma=gamma,
        theta=theta_per_minute,
        vega=vega / 100.0,                        # per 1 vol point
    )


def atm_strike(spot: float, offset: int = 0, step: float = 1.0) -> float:
    """Nearest whole-dollar SPY strike, optionally offset by `offset` strikes."""
    base = round(spot / step) * step
    return base + offset * step
