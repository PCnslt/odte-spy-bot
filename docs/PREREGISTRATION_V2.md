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

## Amendment v3 (2026-07-19, registered before any historical data was seen)

**G1.5 — screening gate on EXISTING data ($0).** Before any ThetaData spend, the same strategy
is screened on the Polygon Starter subscription Shawn already pays for (real 0DTE option minute
**trade aggregates**, 2022-05-16 → present — settled empirically 2026-07-05: that plan has no
NBBO). Fills modeled on trade-bar prices with a spread penalty from a **width(delta,
time-of-day) model calibrated on our own free quote archive** (`src/research/quote_logger.py`
records the real delayed XSP chain bid/ask daily; delay is irrelevant for calibration).

- **G1.5 KILL:** screening net PF < 1.0 on ≥ 350 trades → the strategy dies here; the $80 is
  never spent; pre-registered conclusion stands ("no retail edge at current costs").
- **G1.5 PROCEED:** screening net PF ≥ 1.0 → authorizes (with Shawn's OK) one month of
  ThetaData for the definitive G2. **A G1.5 pass is NOT evidence of edge** — trade-price fills
  overstate quality; only G2 can PASS the strategy.
- G2 criteria are unchanged by this amendment and cannot be relaxed by any G1.5 outcome.
- Honest degradation note: aggregates miss quotes entirely; sparse far-OTM legs print rarely.
  The screen exists only as a cheap one-way filter (kill early / proceed), never as proof.

## Amendment 2 (2026-07-19, registered before any historical data was seen)

Three factual updates from the deep-dive research pass — **no gate criteria change**:

1. **Correction:** XSP complex orders tick **$0.01**, not $0.05 (Cboe release C2022060301;
   SR-CBOE-2025-069). 1×2 ratios (≤3:1) qualify for electronic COB/COA handling. IBKR
   Smart-Routed option-vs-option combos are **guaranteed by default** (KB 1323) — the harness
   may assume atomic net-price fills; legging risk is the broker's.
2. **Prior update (against us), disclosed:** Vilkov's updated sample (through 2026-02) shows
   the put ratio **unconditionally at SR −0.26 (2022–23) and −4.41 (2024–26, n=55, indicative)**.
   Full-sample conditional net 0.93 stands but is carried by earlier years. Gates unchanged —
   this is precisely what they exist to adjudicate cheaply.
3. **Signal-selectivity disclosure:** the ≥2.0 vol-pt VRP gate passed on **85.7%** of the last
   252 trading days (FRED/Cboe data) — it is a tail-avoidance switch, not an edge selector.
   Additionally, peer-reviewed evidence (Papagelis–Dotsis, JFM 2025) finds the VRP concentrated
   **overnight**, thinning what an intraday-only seller can harvest. An **exploratory-only**
   variant (enter prior close, 1DTE, exit next open/noon) may be tabulated alongside G1.5 for
   information; it cannot rescue a FAIL and is not part of any pass criterion.

## Amendment 3 (2026-07-20, registered before any historical data was seen; DeepSeek review incorporated with corrections)

**A. Overnight-VRP arm — UPGRADED to a fully registered second arm** (was exploratory-only).
Rationale: peer-reviewed evidence locates the VRP overnight (Papagelis–Dotsis, JFM 2025).
Spec (corrections to the advisor's draft in bold):
- XSP 1DTE put ratio (buy 1×~25Δ, sell 2×~12Δ), **net credit ≥ $0.10** (a $0.05 credit ≈ $5
  barely clears the ~$4.60 round-trip commissions — too thin).
- Entry 15:50–16:00 ET (randomized) on expiry-eve; exit next day 09:35–09:45 **via GUARANTEED
  combo limit at mid, laddered every 60s, market escalation by 09:45 — never per-leg market
  orders into the open spread** (advisor draft violated our own execution rules).
- Gate: iv_1dte_atm − rv_5d ≥ 2.0 pts, term_slope ≥ 1.0, no event ±1 day, VIX < 30.
- Same G1.5 kill (net PF < 1.0, ≥350 obs) and G2 pass criteria; verdicts independent per arm.

**B. Hedge robustness grid — registered as characterization-only** (run at G2; never alters
pass/fail): wing delta {3,5,7} × roll {weekly, biweekly, monthly} × budget {10,15,20}% — the
12-cell grid as proposed; baseline = 5Δ/monthly/20%.

**C. Sizing correction — L_max must be HEDGE-INCLUSIVE (advisor's $190/unit was wrong).**
A 1×2 put ratio is UNBOUNDED below its short strikes without the wing; any tail-budget sizing
on the unhedged structure is meaningless. Registered rule: **L_max := worst-case settlement
loss of the full position including its long wing (payoff flat below the wing).** At entry the
wing is the nearest listed strike making L_max ≤ 0.5% × NetLiq per unit; if that wing costs
> 25% of the net credit or drives net credit < $0.10, NO TRADE that day. This rule is
self-sizing and involves no data peeking.

**D. Capacity note (corrected arithmetic):** XSP ≈ 745.8 (not 7457); strikes ~$5 apart; naked
12Δ margin ≈ $9–10k/unit (not $25k). At 5% of per-minute volume (ADV 229K → ~587/min) a 90s
entry absorbs ~44 contracts ≈ 14 spreads → strategy caps ≈ **$1.5–2M account size** on XSP;
beyond that = multi-window entries or SPX migration. Conclusion unchanged: no compounding path
through this structure alone.

**E. Tamper-seal hardening (implemented this commit):** the test suite now also asserts this
document still contains the registered criteria verbatim — code constants and pre-registration
text can no longer drift apart silently. The runner already refuses to trade on a red suite.

## Amendment 4 (2026-07-20, registered before any historical data was seen)

**A. Advisor's final triple (25Δ/12Δ/2Δ as 743/740/737 "zero-floor butterfly at a $4.00 net
credit, L_max = $0") — REJECTED as a no-arbitrage violation.** A long butterfly's payoff is
≥ 0 everywhere, therefore it trades at a DEBIT; "collect a credit and never lose" is a pricing
arithmetic error (their wing priced at −$0.18 and a 12Δ put at 51% of a 25Δ put — both broken).
Machine guard added: `src/strategy/structure_math.py::violates_no_free_lunch` — any spec with
credit > 0 and L_max ≤ 0 is auto-rejected, test-sealed (`tests/test_structure_math.py`).

**B. Registered L_max formula** (implements Amendment 3C): for +1 put K_l, −2 puts K_s,
+1 wing K_w (K_w < K_s < K_l): floor = (K_l + K_w − 2K_s) pts; **L_max = max(0, −(credit +
min(0, floor)×100))** dollars/unit. Code-sealed.

**C. Feasibility finding (closed-form sanity only — NOT a backtest, no data consumed):** a
fundable triple EXISTS near **long ≈ 25Δ, shorts ≈ 18–20Δ, wing ≈ 3–5Δ** (≈ $5–6 total span
at July-2026 vol): net credit after wing ≈ $20–35 ≥ $10, hedge-inclusive L_max ≈ $360–420
≤ $500 = 0.5% × $100k. **Deltas are targets; day-to-day selection remains the Amendment 3C
rule** (wing chosen so L_max ≤ 0.5% NetLiq; skip if credit < $0.10 or wing > 25% of credit).
The advisor's "12Δ shorts $3 below a 25Δ long" cannot produce a credit; shorts must sit nearer
the long. If live quotes show no triple satisfying the rule, the registered outcome is
"structure unfundable — no trade," never a relaxed rule.

**D. Margin/scaling corrections to the advisor's table:** with the wing the position is
DEFINED-RISK — margin ≈ max loss (~$0.4k/unit), not $20–21k (that was naked-put margin applied
to a hedged structure). Binding constraints are therefore the capacity ceiling (~44 contracts
per 90s window ≈ 11 units at 4 contracts/unit) and the tail budget — strategy cap ≈
**$0.9–1M account on XSP** at 0.5%/day (11 × ~$420 / 0.005), SPX beyond. Broker margin numbers
remain subject to `whatIfOrder()` verification at G4.

## Sign-off

Engineer of record: Claude (session 61c9c7d9). Owner: Shawn Rahman.
Constants are duplicated in code (`nbbo_backtest.py`) and covered by a test that fails if they
are edited.
