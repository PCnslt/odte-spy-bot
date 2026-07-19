# Pre-registration — Master Plan v2 backtest gate (G2)

**Registered 2026-07-19, BEFORE any NBBO data was purchased or seen.** The git timestamp of this
file is the proof. These criteria may not be relaxed, re-run, or reinterpreted after results
exist. A clean FAIL is a valid, money-saving outcome and triggers the pre-registered conclusion:
*"no retail-executable edge in 0DTE short premium at current costs — stop or pivot."*

## Hypothesis under test

A VRP-conditioned XSP put ratio spread (buy 1×~25Δ put, sell 2×~12Δ puts, net credit ≥ 0),
entered 09:55–10:05 ET only when `iv_0dte_atm − rv_20d ≥ 2.0 vol pts`, `iv_0dte/iv_30d ≥ 1.0`,
no FOMC/CPI/NFP, VIX < 30; exits: 25–40%-of-credit profit target, 14:30 time exit, spot-based
strike defense, 15:30–15:45 flatten; with a standing long 1–7DTE ~5Δ put hedge (15–20% of
premium income) — has positive net expectancy after realistic costs.

## Data

ThetaData Options Standard NBBO quotes (minute or better), XSP + SPX, **2022-05-16 → present**
(the all-weekday 0DTE era only; anything earlier is synthetic by construction).

## Fill model (ORATS-style, fixed in `src/research/nbbo_backtest.py`)

- Decisions on the quote as-of the signal; fills on the **next** quote timestamp.
- Effective fill crosses **75%** of the quoted width for single legs, **53%** for multi-leg
  combos, measured from mid.
- Width multiplied ×**2.0** during the first/last 15 minutes and on event days.
- Costs: **$0.65/contract** + **$1.00 per-order minimum per leg**, open and close.
- Sizing: the live tail-budget rule (0.5% NetLiq / L_max) on a fixed $100k — no compounding.

*Conservatism note (SEC DERA 2025, "Hope at a Reasonable Price"):* measured 0DTE mid-point
limit orders cost ≈ half of crossing and fill 50–62% of the time; complex orders beat legging
(DERA complex-order study). This model always crosses 75%/53% of the width — i.e. it understates
live execution quality. A PASS under it is therefore robust; the model may NOT be made more
optimistic after data is seen. Note for G4: XSP complex orders tick in $0.05 (vs SPY's $0.01),
one more reason XSP spread quality must be measured in paper before live.

## Pass criterion (binary)

**PASS** requires ALL of:
- net profit factor **≥ 1.15**
- bootstrap 95% CI lower bound of $/trade **> $0** (net of all costs)
- **≥ 350** trades in sample

Anything else is **FAIL**. No parameter search on the gated criteria; any exploratory variants
must be labeled exploratory and cannot rescue a FAIL.

## Sign-off

Engineer of record: Claude (session 61c9c7d9). Owner: Shawn Rahman.
Constants are duplicated in code (`nbbo_backtest.py`) and covered by a test that fails if they
are edited.
