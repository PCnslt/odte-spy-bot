# HANDOVER — complete system map for a successor operator or AI agent

Written 2026-07-25. This document plus the repo IS the system. A successor with (a) this
repo, (b) the owner's Mac or a Mac with IB Gateway + the data directory, and (c) the keys
listed in `.env.example` can run everything with zero context from prior conversations.

## What this is

A fully-automated 0DTE options paper-trading system on Interactive Brokers (paper account,
DU-prefixed, port 4002 — **never live money**; `--mode live` is triple-gated and unused).
Currently trading 1-lot SPY credit spreads (v1, no demonstrated edge — an execution
harness) while accumulating evidence for the registered v3 XSP strategy behind sealed,
pre-registered gates. Registered expectation is honest: NO-GO base case
(P(>$1k/mo on $100k) ≈ 3–5%). Nothing here claims an edge; the gates decide.

## Cold start

    git clone https://github.com/PCnslt/odte-spy-bot ~/trading/odte-spy-bot
    cd ~/trading/odte-spy-bot && bash scripts/bootstrap.sh

Deployment path is load-bearing: launchd cannot read iCloud paths; everything expects
`~/trading/odte-spy-bot`. The bootstrap installs deps, dirs, and the launchd agents, and
refuses to finish on a red test suite.

## What is deliberately NOT in git (and how a successor gets it)

| Missing from git | Why | Successor's path |
|---|---|---|
| `.env` (Polygon key) | secret | `.env.example` lists names; owner provisions values |
| `trades.db`, `logs/` (ledger, risk state, quote archive, session logs) | financial runtime state never enters git — policy, repeatedly re-affirmed | lives on the operating Mac; copy the directory if migrating hosts. Without it you lose HISTORY, not FUNCTION — a fresh start re-accumulates |
| `~/.config/odte/dash_auth`, `~/.cloudflared/` | dashboard/tunnel secrets | re-run `bash dashboard/setup_remote.sh` |
| IB Gateway login | owner identity + weekly phone 2FA | the one manual ritual: Sunday evenings |

## Daily operation (all automatic once the Gateway is logged in)

09:25 launchd → `scripts/run_paper_day.sh`: git-sync to origin/main → **pytest gate
(red = flatten + sit out, fail-closed)** → wait for authenticated Gateway → quote logger
(+ 15:50/09:35 VRP snaps) → live loop (30s polls; entries until 15:30; 15:55 flatten) →
EOD: trade report, briefing, cost-meta retrain, death-spiral monitor, **reconcile (ledger
row; book-vs-broker)**, dashboard artifact. Dashboard: `http://127.0.0.1:8090` (view-only,
served from files; optional public tunnel via `dashboard/setup_remote.sh`).

## The invariants that keep it safe (learned from real incidents — do not relax)

1. **Broker is the only source of truth.** `ib.positions()` proves flat; order status
   strings do not. `ib.sleep(0)` never confirms anything (no network round-trip).
2. **Never fabricate a quote.** No two-sided quote → no trade (skip, never guess).
   Delayed snapshots need polling (`leg_quotes` waits up to 4s — Gateway srv-176 lesson).
3. **Fail closed.** Red tests → flatten + sit out. Unconfirmed close → re-send, book
   pnl=NULL, worst-case counts toward the halt. Unmanaged broker positions → entries
   blocked until swept (OPT and STK — assignment leaves stock).
4. **Feed ≠ loop health.** Watchdog catches hung loops; `feed_state.json` + auto-reconnect
   (6 failed polls) catch 'alive but disconnected' (2026-07-24 incident).
5. **Daily risk state persists** (`logs/risk_state.json`) across crash/relaunch — halts
   survive restarts. Paper-account guard: refuses to trade unless account is DU/DF.
6. **Owner rule:** never watch the account via IBKR portal/app during sessions — it
   preempts the bot's market data (competing-session 10197, 2026-07-24).

## Sealed research protocol (the actual point of the system)

- `docs/PREREGISTRATION_V2.md` + `docs/MASTER_PLAN_V2.md` — registered criteria; tests pin
  the constants; **a FAIL is final, no re-runs, no relaxation.**
- G1.5 kill-screen (delayed-data, KILL-only) → **G2-FORWARD** (`docs/PREREGISTRATION_G2FWD.md`,
  free-data forward gate: 60 sessions / 200 registered-structure trades / 40 basis fills;
  live counters on the dashboard) → small live cap only if passed.
- Evidence collectors: quote logger (XSP chain archive), VRP pre-close/next-open snaps,
  `scripts/basis.py`-style delayed→real fill basis once fills accumulate.
- Next scheduled step: **XSP dress rehearsal from 2026-07-27** — `docs/XSP_REHEARSAL_RUNBOOK.md`
  (preflight script, 7 criteria, rollback). Rehearsal book is separate by construction.

## Current state (as of 2026-07-25)

Account $1,000,522 (deposit $1M; the delta ≈ interest minus ~$80 net trading). 7 closed
trades all-time; G2-FWD 5/60 sessions. All 175 tests green at `HEAD`. Known open items:
ib_insync→ib_async migration is gated post-G2 (see AI_REVIEW.md), anomaly staleness wiring
and half-day calendar are pre-live TODOs, tracked in docs/AI_REVIEW.md.

## Incident log (what already went wrong, so you don't repeat it)

All documented in git history + docs/AI_REVIEW.md: phantom-short from trusting order
status (07-08); breach-rode-to-assignment from a fake booked close (07-09); stacked
pre-market covers from a non-idempotent sweep; runner fail-open reverted-and-traded old
code (now fail-closed); 2FA-missed week (07-13..17); delayed-snapshot NaN race (07-20/21);
competing-session + no-reconnect blind afternoon (07-24). Each has a regression test.

## For an AI successor specifically

Read, in order: this file → `docs/MASTER_PLAN_V2.md` → `docs/PREREGISTRATION_V2.md` →
`docs/AI_REVIEW.md` → `scripts/run_paper_day.sh` → `src/main.py`. Honor the seals: the
gate constants are pinned by tests and a FAIL is final. The owner's standing preferences:
terse factual reports, no motivational language, dashboard is view-only, never commit
runtime financial state, commit as the owner (no AI co-author lines), no new paid
subscriptions without explicit approval, paper-only until the gates say otherwise.
