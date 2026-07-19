# Pre-registration: G2-FORWARD (zero-cost forward validation gate)

**Registered: 2026-07-20, before any forward NBBO archive or paper-fill evidence existed.**
Constants are pinned by `tests/test_prereg_g2fwd.py`; editing them after data exists is a
protocol violation. This gate **adds to** the registered plan — it does **not** replace,
relax, or reinterpret gate G2 (docs/PREREGISTRATION_V2.md).

## Why this gate exists

The owner has declined the ThetaData subscription (final, 2026-07-20). The registered G2 —
historical NBBO back to 2022-05-16 — therefore cannot run. The alternatives are (a) no live
money ever, or (b) a slower, weaker, but *honest* forward path. G2-FORWARD is (b), with its
weaknesses priced in rather than ignored.

## Relationship to G2 (binding)

- G2 remains the ONLY gate that can authorize scaling beyond the starter cap below.
- G2-FORWARD PASS authorizes at most a **$100,000 live allocation** (not the ~$1M capacity
  cap), because forward-only evidence with a delayed-data basis is weaker than crisis-spanning
  NBBO history.
- A G2-FORWARD FAIL is final for the tested strategy, exactly like every other gate.
- If ThetaData is ever purchased, G2 runs as registered and **supersedes** this gate in both
  directions.

## Evidence base (all $0, already being collected)

1. **Delayed NBBO archive** — `logs/quotes/*_xsp.csv.gz` (quote_logger, 1/min, delayed 15m).
2. **Snap marks** — `logs/quotes/*_xsp_snap.csv.gz` (15:50 pre-close, 09:35 next-open).
3. **Real paper fills** — `trades.db` (broker-truth entry/exit fills).
4. **Delayed→real basis** — the measured fill-vs-delayed-mid distribution (scripts/basis.py),
   estimating how far real fills land from the delayed mid.

## Pre-registered criteria (pinned; DO NOT EDIT after data exists)

| Constant | Value | Meaning |
|---|---|---|
| `FWD_MIN_SESSIONS` | 60 | distinct sessions with logger coverage in the sample |
| `FWD_MIN_TRADES` | 200 | real paper round-trips of the registered structure |
| `FWD_MIN_PF` | 1.15 | profit factor on net P&L after all haircuts (same bar as G2) |
| `FWD_CI_LOWER_GT` | 0.0 | seeded-bootstrap 95% CI lower bound of mean $/trade must exceed |
| `FWD_COST_QUANTILE` | 0.90 | per-bucket half-spread input = q90 of tradeable widths, never the mean |
| `FWD_BASIS_MODE` | "p90" | delayed→real basis haircut applied at its 90th percentile |
| `FWD_MIN_BASIS_N` | 40 | fills required before the basis estimate is usable; gate cannot run earlier |
| `FWD_UNTRADEABLE_MAX` | 0.20 | if >20% of sampled quotes are one-sided/crossed, the day is untradeable-heavy and EXCLUDED from capacity claims (never from P&L) |
| `FWD_BOOT_N` / `FWD_BOOT_SEED` | 10000 / 20260720 | deterministic verdict |
| `FWD_ALLOC_CAP_USD` | 100000 | maximum live allocation a PASS can authorize |

PASS requires **all** legs. Anything else is FAIL. One run, when
`FWD_MIN_SESSIONS ∧ FWD_MIN_TRADES ∧ FWD_MIN_BASIS_N` are first simultaneously satisfied;
no early peeks, no re-runs on the same sample.

## Honest limitations (accepted at registration)

- **No crisis in sample.** Forward-only data almost certainly contains no 2020/2018-class day;
  tail risk is unmeasured. This is priced in via the allocation cap, not hand-waved.
- **Delayed quotes.** The basis haircut is an estimate of the delay cost, not a measurement of
  each fill's true NBBO at decision time.
- **Slow.** At 1–4 trades/day, `FWD_MIN_TRADES=200` ≈ 3–9 months of sessions.
- **Weaker power.** ~200 trades cannot distinguish PF 1.15 from PF ~1.0 with high confidence;
  the CI-lower leg carries most of the discrimination burden.

## What this gate does NOT do

- Does not modify `src/research/nbbo_backtest.py` or its pinned constants.
- Does not permit simulated option prices anywhere in its evidence base.
- Does not authorize any spend, any non-XSP instrument, or any size above the cap.
