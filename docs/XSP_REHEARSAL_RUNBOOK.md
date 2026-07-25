# XSP Dress Rehearsal Runbook — starts Mon 2026-07-27

Ten paper sessions trading **XSP** (Mini-SPX, European, cash-settled) through the full live
stack, against a **separate book**, to prove the execution layer before the v3 strategy ever
touches it. Real 1-lot combo orders, all brakes on. The SPY book and gate evidence in
`trades.db` are untouched by design.

## What changes (and what doesn't)

| | Normal session | Rehearsal |
|---|---|---|
| Command | `python -m src.main --mode paper --daily` | same **+ `--rehearsal`** |
| Underlying | SPY `Stock` | **XSP `Index` (CBOE)** |
| Options | SPY | **XSP, tradingClass `XSP`** |
| Trade log | `trades.db` | **`trades_rehearsal.db`** |
| Risk state | `logs/risk_state.json` | **`logs/risk_state_rehearsal.json`** |
| Everything else | — | identical: brakes, defense, flatten, watchdog, reconcile |

Config source: the `rehearsal:` block in `config/config.yaml` (activated ONLY by the flag;
the base config stays SPY, so a plain launchd session is unaffected).

## Monday 07-27 sequence

1. **09:00 — preflight (read-only, no orders):**
       venv/bin/python scripts/xsp_preflight.py
   PASS required on all checks (Gateway auth; XSP Index qualifies; today's 0DTE XSP chain
   qualifies with tradingClass XSP; delayed quotes present; Polygon XSP aggregates
   reachable). Any FAIL → do not start the rehearsal; run the normal SPY session instead.
2. **09:25 —** the launchd runner starts the NORMAL SPY session as always (unchanged).
   Start the rehearsal loop manually alongside it, in a terminal:
       cd ~/trading/odte-spy-bot && venv/bin/python -m src.main --mode paper --daily --rehearsal
   (Separate client ids; the two loops and the quote logger coexist — verified pattern.)
3. **15:55 —** both loops flatten themselves; EOD reconcile runs per normal.
4. **After close —** bullet report against the success criteria below.

## Success criteria (all 7, over ≥10 sessions)

1. 100% of near-the-money XSP contracts qualify (`qualifyContracts` non-empty).
2. Every combo order reaches a terminal state; every fill confirmed via `ib.positions()`;
   zero phantom fills.
3. **Zero STK-orphan false positives** — XSP is cash-settled; if the STK sweep ever fires
   for XSP, a contract is misqualified. Hard fail.
4. `leg_quotes` returns two-sided XSP quotes each session (delayed OK).
5. Polygon XSP option aggregates non-empty for traded strikes (G1.5 input sanity).
6. EOD reconcile: rehearsal book == broker, every session.
7. ≥1 expiry crossed with clean cash settlement (no assignment, no residual position).

## Rollback plan

Catastrophic failure in session 1 (mis-routed order, unqualifiable chain, phantom fill):
1. Stop the rehearsal loop (Ctrl-C — SIGTERM triggers its emergency drain + flatten).
2. Confirm flat: `venv/bin/python -m src.main --flatten --mode paper` (exits non-zero if
   not provably flat — investigate before walking away).
3. No revert needed: the normal SPY session was never touched (separate process, book, and
   risk state; base config unchanged). The rehearsal simply doesn't run again until the
   defect is fixed and its regression test lands.
4. `trades_rehearsal.db` keeps the evidence of what went wrong; it never pollutes gates.

## Verification command (run any time)

    venv/bin/python -m pytest -q      # includes rehearsal wiring + XSP contract tests
