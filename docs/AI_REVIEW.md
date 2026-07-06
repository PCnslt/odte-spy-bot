# AI Architecture Review — evaluation of the "AI-first cognitive layer" proposal

*2026-07-05. An external proposal (DeepSeek) suggested overhauling the intelligence layer
with Hugging Face transformers, LLMs, embedding memory, and adaptive exits. This document
records what was adopted, what was rejected, and why — so future contributors don't
re-litigate hype.*

## The proposal's core claim — and why it's right

> "For a credit-spread seller, directional accuracy is secondary to volatility and range
> forecasting."

**Correct, and now implemented.** A short vertical wins when the underlying *stays out of
a zone*; the natural target is the forward maximum excursion, not next-bar direction.
Empirical confirmation from this repo's own data: on identical features, LightGBM fits
the direction target with **14 boosting rounds** (nearly nothing to learn) but the range
target with **119 rounds** (real structure). Volatility is forecastable; direction mostly
isn't. This one insight drove the whole upgrade.

## Adopted (with the mechanism actually used)

| Proposal | Implementation |
|---|---|
| Range forecasting as the target | `RangeForecaster` (LightGBM regression) on forward max-excursion labels (`make_range_labels`), retrained nightly with an only-if-better MAE gate |
| Range-aware strike selection | `dynamic_short_otm`: short strike beyond expected range × safety (1.25×), floored at static 0.2%, **skip entirely** if the safe strike exceeds 1% OTM |
| Volatility-aware exits | `strike_defense` exit: close when spot reaches within 0.1% of the short strike — mechanics-based, fires before the premium stop when the strike is threatened |
| Liquidity awareness | `liquidity_ok`: sum of leg half-spreads must be ≤ 25% of credit, from **real IBKR leg quotes**; entry limit priced at 95% of quote-mid credit instead of 90% of last |
| Event/context awareness | `EventGuard` + `config/events.yaml`: FOMC/CPI days block entries or double the range-safety margin. Local file, no scraping in the loop, fail-safe on malformed input |

All are fail-safe: missing model → ATR-scaled range estimate; missing quotes → configurable
(default: proceed as before); empty calendar → no-op.

## Rejected (with reasons, not vibes)

1. **Replace LightGBM with HF transformers/LLMs for the signal.** For small tabular
   datasets (~10⁵ rows, 19 features), gradient-boosted trees dominate transformer
   alternatives on accuracy, latency, interpretability, and robustness — consistently, in
   every credible tabular-ML benchmark. An LLM in a 30-second loop on a laptop adds
   latency, nondeterminism, and new failure modes, and brings zero information the
   features don't already contain.
2. **FinBERT / LLM news parsing.** The system has **no licensed news feed**. A sentiment
   model with nothing to read is decoration. If a real-time news source is ever added,
   revisit — the lazy-loading `sentiment_analyzer.py` hook already exists.
3. **Embedding-based "semantic memory" of market states.** Stripped of language, this is
   k-nearest-neighbors on feature vectors — a *worse* estimator of the same mapping
   LightGBM already learns, at higher cost. The legitimate kernel (use history of similar
   states) is exactly what supervised training on those features does.
4. **QLoRA/PEFT fine-tuning.** There is no labeled text corpus to fine-tune on. Fine-tuning
   a language model on price bars is a category error.
5. **Replacing the `learning/` layer.** The walk-forward harness and the
   retrain-only-if-better gate are the system's most valuable assets — they are the reason
   the losing strategy was caught. They stay.

## The honest scoreboard rule — and its first verdict

No intelligence-layer change ships as default until the walk-forward OOS harness
(`python -m src.research.spreads --smart`) shows it does not degrade the baseline.

**First application (90 days, 9 folds, real option legs):**

| Variant | Trades | Win | PF | $/trade | MaxDD |
|---|---|---|---|---|---|
| Baseline (static strikes) | 107 | 63.6% | **0.94** | **−$1.04** | $403 |
| + range-placed strikes | 105 | 62.9% | 0.84 | −$2.17 | **$376** |
| + strike-defense exit | 124 | 44.4% | 0.50 | −$10.58 | $1,328 |
| + both | 106 | 61.3% | 0.77 | −$3.25 | $386 |

Verdict: **both features ship default-OFF** (`intelligence.use_range_strikes`,
`intelligence.defense_enabled`). Range strikes trade expectancy for drawdown (wider strike
→ smaller credit → fixed costs dominate; the test window had no crash to reward the
safety). Defense exits near 0.2%-OTM strikes are a whipsaw machine — spot brushes the
strike constantly and the exit realizes losses that would have recovered. The plausible
home for both is *event days and high-vol regimes*, which the event guard can invoke.
The liquidity gate, quote-mid entry pricing, event guard, and nightly range-model training
shipped ON — they avoid costs or add information without adding risk.

**Meta-lesson:** every one of these ideas sounded obviously right. Two of three degraded
the strategy out-of-sample. This is why the harness exists, and why proposals — human or
AI — get tested, not trusted.

---

# Round 2 — the "5 remaining edges" proposal (2026-07-05)

The second proposal was well-calibrated (numeric, tabular, testable). Dispositions:

| # | Proposal | Disposition |
|---|---|---|
| 1 | Predictive regime risk-multiplier (sizing) | **Rejected — no room to act.** At $10k equity, 2% risk (~$200) < 1 contract's max loss (~$450) → qty is pinned at 1 on every trade. Sizing intelligence is a no-op until equity ~5×. |
| 2 | IV/RV premium gate | **Built as the EV gate; failed OOS; ships OFF.** See below. |
| 3 | Predicted exit slippage | **Built the mechanical version: limit-first exits (ON).** No historical quotes exist to train a slippage model, so prediction would be fiction. Instead: non-urgent closes (profit target / time stop) go LIMIT at the current mid and escalate to market after 120 s; stops/defense/flatten stay market. Judged by real paper fills. |
| 4 | K-Means/GMM market-state clusters | **Rejected — underpowered.** 107 OOS trades across k clusters = noise-level win rates per cluster; supervised breach/direction models already learn state→risk with better statistics. |
| 5 | Dynamic profit target (theta/IV-conditioned) | **Deferred.** Multi-arm exit testing needs more trades than exist; queued until the paper account accumulates ≥100 real spreads. |

## The EV gate: built, corrected once, still failed — a useful negative result

Design: nightly breach classifiers P(spot reaches the short strike within 120 min), gate on
exit-structure EV = credit × [pt(1−p) − (stop−1)p] (positive iff p < 1/3 at pt=50%/stop=2×).

- **First version (width-based EV) blocked 100% of entries** — instructive bug: max loss
  needs expiry through the long strike, but our stop fires on a touch, so loss-given-breach
  ≈ 1× credit, not the $5 width. EV must match the exit structure it lives under.
- **Corrected version: 53/107 trades passed the gate — the worse half.** OOS PF 0.70 vs
  baseline 0.94, −$5.69/trade vs −$1.04. The breach probabilities are real (the model
  learns *that* vol clusters) but do not *rank* entry quality over this window.

| Variant (90d OOS) | Trades | Win | PF | $/trade |
|---|---|---|---|---|
| Baseline | 107 | 63.6% | **0.94** | **−$1.04** |
| + EV gate (corrected) | 53 | 60.4% | 0.70 | −$5.69 |

Ships implemented but **OFF** (`intelligence.use_ev_gate`). The breach models still train
nightly — their probabilities are logged and may prove useful once more data exists to
evaluate them on (e.g., regime-conditional gating with a year of history).

## Running tally of the gauntlet

Ideas proposed (by humans and AIs) that *sounded* right: **6**. Survived out-of-sample:
**0 strategy changes**; survivors are cost-avoidance mechanics only (liquidity gate,
quote-mid entries, limit-first exits, event guard). The baseline strategy remains the
best known configuration. This is what edge-hunting actually looks like.

---

# Round 3 — recycled ideas, one valid thread (2026-07-05)

Round 3 re-proposed regime sizing (still impossible: qty pinned at 1 at $10k), IV/RV
gating (no IV history exists → cannot pass the gauntlet), Optuna profit targets (tuning on
the same overused 90-day window = overfitting by construction), and a fill-quality model
"trained on IBKR paper fills" (of which there were **zero** at proposal time).

**The valid thread was #5: prepare for when data exists.** Generalized and built:

1. **TradeLog** (`src/utils/trade_log.py`, `trades.db`): every live trade records its full
   decision context — regime, ML prob, range forecast, breach probabilities, **short-leg IV
   (Polygon snapshot at entry) vs realized vol**, RVOL/ATR/session-time — plus execution
   truth: estimated vs filled credit, estimated vs filled exit cost, limit-vs-market path.
   This table is the training set that IV/RV gating, fill-quality prediction, dynamic
   profit targets, and regime clustering were all missing. `python -m src.utils.trade_log`
   prints per-regime / slippage / IV-bucket stats, with a hard "n<30 = noise" warning.
2. **Consecutive-loss brake** (`risk.limits.max_consecutive_losses: 6`): pauses new entries
   after 6 straight losses — a faster circuit than the 20-trade self-corrector window.
   Risk-reducing only, no model, resets on any win. Addresses the one fair criticism
   (reactive risk) with plumbing instead of an unpowered model.

**Standing rule for future rounds:** proposals that need data the system doesn't have are
neither accepted nor rejected — they are *instrumented*, and the TradeLog decides later.

---

# Round 4 — convergence (2026-07-05)

Round 4 (after receiving our critique + SYSTEM.md) was the first genuinely useful round:
it stopped proposing signal models, addressed statistical hygiene, and pointed at
structure-over-signal. Dispositions:

| Item | Disposition |
|---|---|
| Validation protocol (holdout + correction + pre-registration) | **Adopted** → `docs/RESEARCH_PROTOCOL.md`. Holdout corrected: DeepSeek said "reserve the last 150 days" — the last 90 days are already burned; reserved **2025-01-02→2025-06-30** instead. |
| Structure > signal ($10-width first) | **Tested (exploratory): PF 1.02, +$0.52/trade — first PF>1.0 in system history.** Pre-registered H2 (holdout confirmation) + H2b (live width A/B, running). |
| Pre-registered hypotheses | Adopted as H1 (IV/RV), H2/H2b (width), H3 (limit exits), H4 (PT A/B, queued). |
| Data spend | Agreed: stay at $29/mo; the TradeLog is better evidence than $199 historical NBBO at this trade count. |
| Failure mode #1 (silent auth expiry) | **Built:** `--healthcheck` (authenticated account check) now gates the morning session in `run_paper_day.sh`. |
| Failure mode #2 (corrupted retrain) | **Built:** bar-count abort + 2×-median sanity floor over `models/metrics_history.json`, ahead of the only-if-better gate. |
| Failure mode #3 (overnight gap blows out positions) | **Premise corrected:** 0DTE positions never survive overnight here (15:55 flatten, same-day expiry). The real risk is *entering* a violent post-gap open → built the **opening-gap guard** (no new entries when |gap| ≥ 1%). |

Round-4 lesson: adversarial review works in both directions — the external model improved
after being confronted with its record, and its structural instinct produced the first
PF>1.0 exploratory result. The protocol now exists to keep that result honest.
*(Round-5 postscript: the corrected fill model revised that PF>1.0 down — see below.)*

---

# Round 5 — full code audit: findings, fixes, and the corrected evidence (2026-07-05)

DeepSeek audited the complete codebase (46 files). All 7 of our self-flagged suspects were
confirmed; its independent additions were mostly good. Dispositions:

| Finding | Verdict | Action |
|---|---|---|
| C1 session-boundary label leakage | **Confirmed — and extended**: `make_labels` (directional) had the identical bug DeepSeek missed | `_same_session_mask` in all THREE label functions (ET-date bounded windows) |
| C2 same-day cache poisoning | Confirmed | Completeness guards: never serve/write caches for an unfinished session (`_day_is_complete`, `_cache_fresh`, incl. `load_bars`) |
| M1 stale long-leg ffill | Confirmed | `merge_asof` with hard 5-min staleness tolerance; unpriceable rows dropped |
| M2 exits on last-trade prints | Confirmed | Backtest: stops trigger on PESSIMISTIC intrabar cost (short-high/long-low), fill at worst(stop, bar); TP stays on closes. Live: `spread_close_cost` is quotes-first, bars fallback |
| M3 ungated git pull | Confirmed | pytest gate after pull; revert to pre-pull commit on failure |
| M4 combo sign logic + sleep(2) | **Partially rebutted**: the claimed "PM thinks it's open, broker has nothing" cannot happen — the unfilled-entry state machine cancels at 3 min | Kept the polish: bounded `waitOnUpdate` loop, raw-fill-price logging + sign-anomaly alarm on first real fills |
| m1 width-A/B min-credit bias | Confirmed | Counterfactual logging: the OTHER arm's credit resolved & recorded per entry (`alt_width_credit_est`) |
| m2 timezone assumption | Accepted as risk | Fail-closed startup check: refuse to run if host clock ≠ ET (override env var) |
| m3 sleep(2) race | Merged into M4 | — |
| m4 fake-VIX "attack" | **Rejected** — a user faking their own data hurts only themselves | Doc note only |
| m5 events calendar fragility | Accepted | `python -m src.utils.events --validate` CLI (stale/invalid entries) |
| m6 real-data integration test in CI | **Rejected** — network in CI = flaky CI | Optional local run documented |
| O1 doc says quotes, code used bars | Confirmed | Fixed the CODE (quotes-first exits), doc now true |
| O2 SelfCorrector dead code | Confirmed | **Removed** (module + test); consecutive-loss brake + nightly retrain cover it |
| O3 range model trained but unused | Working as intended | Telemetry + `--smart` research arm; documented |
| O4 two backtesters | Confirmed | `backtest.py` marked DEPRECATED (kept for the long-premium record + walkforward) |

## The corrected evidence — old numbers were optimistic

C1+M1+M2 change labels AND fills, so all OOS results were re-run on the corrected harness
(same 90-day window; models retrained on session-bounded labels):

| Variant | Old harness | **Corrected harness** |
|---|---|---|
| $5 baseline | 107 tr, 63.6%, PF 0.94, −$1.04 | **126 tr, 60.3%, PF 0.70, −$6.62** |
| $10 width | 106 tr, 65.1%, PF 1.02, +$0.52 | **122 tr, 66.4%, PF 0.90, −$2.30** |

**What changed and why:** stops now fill at the bar's worst price instead of politely at the
stop level, and stale-leg mispricing no longer smooths exits. The old harness flattered
fills; the "first PF > 1.0" headline is **retracted**.

**What survived:** the STRUCTURAL effect. $10 width beats $5 by +0.20 PF / +$4.32 per trade
under the harsher model (it was only +0.08 / +$1.56 under the old one) — wider spreads are
robustly better *relative*, in both fill regimes. And reality likely sits BETWEEN the two
harnesses: the corrected one is deliberately pessimistic (worst-intrabar stop fills PLUS
flat slippage on top). The TradeLog's est-vs-fill columns exist precisely to locate where
real fills fall; the live width A/B continues unchanged.

**Protocol amendment (made before any holdout contact):** H2's confirmatory test runs on
the corrected harness with acceptance **PF > 1.00** (break-even under pessimistic fills =
genuine edge under conservative assumptions). Recorded in RESEARCH_PROTOCOL.md.

---

# Round 6 — pre-emptive self-audit before the next external round (2026-07-05)

Four holes found internally, one of them fatal to the whole operation:

1. **FATAL: launchd cannot read iCloud Drive.** The repo (and the runner script) lived in
   `~/Library/Mobile Documents/...`; launchd's load-time run exited 127 ("can't open input
   file"). **The 9:25 session would never have started** — discovered only because the new
   crash-only KeepAlive ran the job at load. Fix: the RUNTIME is now a plain clone at
   `~/trading/odte-spy-bot` (also removes the iCloud-syncs-SQLite-mid-write corruption
   hazard); the iCloud copy is the dev workspace; the runner derives paths from its own
   location. Verified: launchd load-run executes, weekend guard exits 0, no restart loop.
2. **Crash recovery:** a mid-session crash left real positions unmanaged (restart had no
   memory of them, and launchd wouldn't restart at all). Now: KeepAlive restarts on crash
   only, and startup calls `flatten_orphans()` — any account position this process didn't
   open is closed at market, fail-closed, with an alert.
3. **H3 was unanalyzable as built:** market exits recorded ESTIMATES; actual market fills
   were never captured, so limit-vs-market had no market-side data. All closes (limit,
   escalated, urgent market) now flow through one pending-close tracker that records the
   real fill; the interrupt path drains briefly to catch in-flight closes.
4. **Train/serve distribution mismatch:** Polygon aggregates include pre/after-hours bars;
   the live IBKR feed is RTH-only. Models were trained on a distribution the live system
   never sees, and labels could include after-hours moves. `_rth_only()` now filters at the
   `load_bars` choke point (caches stay raw).

## Harness v3 (RTH) — the evidence, revised again

| Variant | v1 original | v2 pessimistic fills | **v3 + RTH-matched** |
|---|---|---|---|
| $5 baseline | PF 0.94, −$1.04 | PF 0.70, −$6.62 | **PF 0.56, −$10.86 (104 tr)** |
| $10 width | PF 1.02, +$0.52 | PF 0.90, −$2.30 | **PF 0.63, −$10.26 (96 tr)** |

Every honesty correction has made the strategy look worse — the pattern itself is the
finding: **the original harness's near-break-even story was substantially fill-model and
data-hygiene artifact.** The width effect, "+0.20 PF, robust" under v2, is **+0.07 under
v3 — no longer distinguishable from noise.** Claims that survive all three harnesses:
selling >> buying, and costs dominate. Claims that don't: any near-break-even PF, the
width effect's magnitude. v3 is the authoritative harness going forward (it matches what
the live system actually sees); reality still likely sits between v1 and v3 fills, and the
TradeLog's est-vs-fill data is the instrument that will say where. The live width A/B
continues — real fills outrank all of this.

---

# Round 7 — the "MASTER BUILD PROMPT" regression, rejected; the diagnostic that mattered (2026-07-05)

## The proposal: rejected wholesale

Round 7's external advice was a full-architecture rewrite: HF time-series transformers
replacing LightGBM, Composio as the data/execution layer, DynamoDB replacing SQLite, FinBERT
news sentiment, tiered risk multipliers, weekly K-Means. Verdict: **a regression to round 1**,
re-proposing ideas this project already killed with evidence, plus new failures of arithmetic:

- HF free inference = ~1,000 req/month ≈ 45/trading day; the loop polls ~720×/day. DOA.
- QLoRA-tuning a 4B model nightly on a free 2-core GitHub runner: not feasible.
- ~7,500 RTH bars/month is orders of magnitude short for transformer forecasting.
- Composio is agent-session tooling; inserting a SaaS round-trip into a 30 s trading loop
  adds latency + failure modes; multi-vendor bar mixing corrupts feature pipelines.
- DynamoDB for a single-host system replaces a working zero-dependency SQLite with network
  faults and cloud credentials, for nothing.
- FinBERT: still no news feed (third rejection). Risk multipliers: qty still pinned at 1
  (fourth). K-Means on trades: live trade count at proposal time was zero.

It also answered none of our Q1–Q4. The effort went into answering Q1 ourselves instead.

## The Q1 diagnostic — where the $10.86/trade actually goes

Five-arm matrix, v3 harness, $5 width, same window (runs are DIAGNOSTIC, not variants):

| Arm | Trades | Win | PF | $/trade |
|---|---|---|---|---|
| 1. Baseline (costs + 2× stop) | 104 | 59.6% | 0.56 | −$10.86 |
| 2. No costs (stop on) | 104 | 62.5% | 0.74 | −$5.62 |
| 3. No stop (costs on) | 76 | **81.6%** | **0.85** | **−$2.90** |
| 4. No costs + no stop | 78 | 82.1% | **1.22** | **+$3.46** |
| 5. RANDOM entries (costs + stop) | 125 | 62.4% | 0.62 | −$8.87 |
| 6. No stop, $10 width (costs on) | 72 | 79.2% | 0.78 | −$6.13 |

**Attribution:**
- **(a) Entries carry ZERO information.** Random entries (PF 0.62) ≥ the full signal stack
  (0.56). Every "improve the signal" proposal was aimed at a component that does nothing.
- **(b) The 2× stop was the biggest self-inflicted wound.** Removing it: +0.29 PF, win rate
  60→82%. 0DTE spread gamma noise trips premium stops constantly; the defined-risk structure
  (max loss = width − credit) already IS the stop.
- **(c) Costs ≈ $6.36/trade** (arm 3 vs 4): ~$2.60 commissions + modeled slippage.
- **The gross engine is real but small:** +$3.46/trade before costs (PF 1.22). Viability =
  keeping arm-3 mechanics while closing ~$3/trade of cost — which is precisely what the
  live-only improvements (limit-at-mid exits, quote-mid entries) attack, and what the
  TradeLog measures from the first fill.
- **Width flips under no-stop:** $5 (0.85) beats $10 (0.78) — unstopped tails punish width.
  H2b's live A/B continues (pre-registered; live evidence is fresh), with priors updated.

## Master decisions (Claude, engineer-of-record)

1. **Live exits: hold-to-target** (`stop_mult: 999`) — stop only at the structural max-loss
  cap. Effect size (+0.29 PF), sign-stable across v2/v3 fill models, mechanism understood.
2. **H5 pre-registered, replacing H2's holdout slot** (H2's exploratory basis collapsed):
  $5 width, no premium stop, v3 harness, holdout look, accept PF > 1.00.
3. DeepSeek's architecture: not implemented. This document is the rationale.
4. Window-wear count after diagnostics: ~19 looks. The 90-day window is retired for
  anything but sanity checks; evidence now comes from the TradeLog and (once) the holdout.

---

# Round 8 — DeepSeek engages properly; dispositions (2026-07-05)

Round 8 answered the questions directly — its best round. Dispositions:

| Item | Verdict | Action |
|---|---|---|
| Q1 steelman vs no-stop (calm-regime bias, pessimistic-fill amplification, survivor composition) | **Good critique, accepted** | Its proposed check executed: three-arm exit test over 180 days — the Jan–Apr 2026 folds were never OOS-judged (fresh window). Results below. |
| Q2 tail arithmetic + **H6: stop at 50% of width** | Coherent, testable | `stop_width_frac` implemented (config + harness + live path, `stop_cost()` helper); H6 arm included in the 180-day test; registered in the protocol pending that evidence. |
| Q3 H4 redesign (40%/60% PT, n≥50/arm, kill at PF<0.5@n≥30, start after H2b) | Accepted | Protocol H4 updated. |
| Q4 cost-lever ranking | Mostly sound | Its "novel" bounce-point lever ≈ limit-at-mid (already live). Its claim that delayed data is irrelevant is WRONG for quotes-first exits — 15-min-delayed quotes pricing stop decisions is a real gap; flagged to the operator (real-time IBKR data ≈ $5/mo). |
| Q5 month-1 decision table | Accepted | Adopted into the protocol (appendix) with its thresholds. |
| Q6 #1 stale-bar exit fallback | **Confirmed** | Freshness guard: fallback bars older than 180 s → poll skipped. |
| Q6 #2 rv_60m mixes sessions near the open | **Confirmed** | rv_60m now computed from TODAY's bars only. |
| Q6 #3 searchsorted index mismatch | **REBUTTED** | `idx_utc` IS `bars.index` (spreads.py assigns it directly); `exit_ts` is a tz-aware Timestamp and `DatetimeIndex.searchsorted` is unit-aware — that was precisely the round-5 fix. No two indices exist. |
| Q6 #4 migration drift | Observational | Policy comment added; no action. |
| Q6 #5 fill-below-estimate raises max loss vs sized risk | **Valid but a no-op today** | qty is pinned at 1; P&L already uses actual fills (`spread_fill_status` updates credit). Revisit when sizing unpins (~5× equity). |
| Q6 #6 interrupt orphan gap | Partially valid (its expiry scenario confused; shorts expiring OTM is fine) | Kept the useful part: post-drain `flatten_orphans()` sweep on interrupt + `--flatten` recovery CLI. |
| Exploratory liquidity-gate/entry-window sweeps | **Deferred** | More window looks for effects the TradeLog will measure for free with real fills. Wait for n≥100. |

## R8 appendix — the three-arm exit test (fresh-window verdict, 2026-07-05)

Decision rules R1–R4 were pre-committed in writing BEFORE results existed (see round-9
prompt). 180 days, 21 folds; "fresh" = Jan–Apr 2026 test folds never used in any prior OOS
judgment. Disclosure: per-fold PF was not recoverable from run logs, so R1/R2 were applied
on segment $/trade + pooled PF — which agree unambiguously.

| Arm | Fresh tr | Fresh win | Fresh $/tr | Pooled PF |
|---|---|---|---|---|
| A. 2× credit stop | 111 | 61.1% | −$11.85 | 0.59 |
| B. hold-to-target (live) | 87 | **78.1%** | **−$8.57** | **0.69** |
| C. H6 50%-of-width stop | 93 | 74.3% | −$15.07 | 0.61 |

**Rulings:** R1 → hold-to-target RETAINED (beats 2×-stop on fresh data on every metric).
R2 → **H6 REJECTED** — worst arm on fresh folds; it does not take the spare holdout look;
live config unchanged. R3/R4 not triggered.

**Scorekeeping both ways:** DeepSeek's H6 failed empirically, but its steelman of the
no-stop effect was substantially right — the advantage shrank from +$7.96/trade (retired
window) to **+$3.28/trade (fresh)**: sign stable, magnitude inflated by the calm window,
within its predicted 0.05–0.10 PF haircut. Consequence: **H5's holdout prior weakens** —
at fresh-window expectancy (−$8.57/trade historical-harness), PF > 1.00 on the holdout is
unlikely unless live fills prove materially better than v3's pessimistic model. H5 stays
scheduled (after ≥1 month of live A/B) per protocol; the TradeLog's est-vs-fill data is
now decisive for whether the strategy family survives at all.

---

# Round 10 — the "next-gen enhancement" blueprint: graded, one build (2026-07-05)

The proposal was DeepSeek's most sophisticated — much of it is this project's own protocol
reflected back (pre-registration, cost realism, fail-closed, the holdout). Grading against
the standing tests (data exists? powered at our n? fits the loop? already tried?):

| Proposal | Verdict | Reason |
|---|---|---|
| **GEX / dealer-gamma telemetry** | ✅ **BUILT (instrument-first)** | The one new idea that is real AND feasible today: chain snapshot with greeks+OI is Starter-entitled (verified live). `compute_gex` + per-session capture + TradeLog columns + **H7 pre-registered** with a written directional prediction. No gating until H7's test. |
| Adversarial stress drill | ✅ Built (as software test) | Synthetic-extreme inputs confined to `tests/` (policy-compliant): the black-swan drill verifies anomaly-HALT, gap guard, and strike-defense fire in concert — the layers protecting a no-stop book. |
| Order-flow / tape / depth features | ❌ Rejected | Data does not exist on any owned entitlement (ticks need Developer; depth needs far more). |
| HAR-RV intraday vol forecaster | ⏸ Deferred | Same target as the existing (telemetry-only) range forecaster; adding a second unconsumed vol model creates maintenance, not evidence. Revisit if H7/H1 analyses show vol-forecast columns carry signal. |
| Regime HMM → trade-allowed flag | ⏸ Deferred | Needs labeled outcomes (live trades). Crude gates already exist (event/gap/anomaly/brake); month-1 per-regime stats are the prerequisite data. |
| Multi-objective strike/width optimizer | ❌ Rejected for now | Its inputs (P(touch), conditional loss, slippage model) are exactly the models that failed or don't exist yet. An optimizer over garbage inputs is a garbage amplifier. Width A/B is the honest primitive. |
| Meta-labeling | ⏸ Parked (protocol parking lot, n ≥ 200) | Legit technique; zero training rows today. |
| Slippage predictor | ⏸ Parked (n ≥ 200 fills) | Already instrumented; model later. |
| RL exit policy | ❌ Rejected | Violates the standing no-RL rule (which DeepSeek itself set in round 4). |
| Kelly sizing | ❌ Rejected (5th sizing rejection) | qty pinned at 1; Kelly additionally requires calibrated win-prob estimates that don't exist. |
| DynamoDB / MLflow / dashboards | ❌ Rejected | Single-host system; SQLite + logs + this document ARE the registry. |
| Historical crash replay (2018/2020) | ❌ Not possible | Data entitlement reaches ~2024-07 only. |
| Shadow-mode model deployment | ⏸ Deferred | Good pattern; no candidate model to shadow. Reconsider with the first parking-lot model. |
| "Deep question" (is retail 0DTE viable?) | Already answered by the system | Under pessimistic fills: no. Under real fills: being measured from the first live session. The framework is the durable asset either way; any pivot (45-DTE, etc.) goes through this same protocol. |

## Risk assessment of the adopted design (no bullshit)

- **The range model can be wrong at exactly the wrong time.** Vol forecasts fail hardest
  at regime breaks. Mitigations: safety multiplier, skip-above-cap, anomaly halt, daily
  loss halt, defined-risk structure (max loss capped by construction).
- **Dynamic strikes reduce credit.** Wider strikes = smaller premiums = min-credit skips
  more entries. Fewer, better trades is the intent; the OOS harness verifies it isn't
  "no trades at all."
- **Strike-defense exits can whipsaw** — closing on a touch that would have recovered.
  This buys tail protection with expectancy; the OOS comparison prices that trade-off.
- **Quote-based gates depend on IBKR data entitlements.** With delayed data, quotes are
  delayed too; gates use what exists and degrade explicitly, never silently.
