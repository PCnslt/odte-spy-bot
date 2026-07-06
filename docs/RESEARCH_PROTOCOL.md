# RESEARCH PROTOCOL — the constitution for the next 12 months

*Adopted 2026-07-05. Any amendment must be a commit that states what changed and why.
Purpose: stop the quiet erosion of statistical validity that killed every casual quant
project before this one. Rules here bind all future research, human- or AI-proposed.*

## 1. Data domains and their rules

| Domain | Range | Status | Rules |
|---|---|---|---|
| **Worn window** | ~2026-04-01 → 2026-07-02 (the 90-day OOS set) | **Exploratory only** | Has judged ~9 experiments. Results on it are hypothesis-generating, never confirmatory. |
| **Exploratory pool** | 2024-07 → 2024-12 and 2025-07 → 2026-03 | Open | For walk-forward exploration. Budget: **max 10 distinct variants per year**; every test logged in `docs/AI_REVIEW.md`. |
| **RESERVED HOLDOUT** | **2025-01-02 → 2025-06-30** (~124 trading days) | **NEVER TOUCHED** | Not downloaded, not inspected, not summarized until a confirmatory test runs. Budget: **2 confirmatory looks total**, each pre-registered here first. Expires from the 2-year data entitlement ~Jan 2027 — confirmatory tests must run before then. |
| **Live TradeLog** | accumulating from 2026-07-06 | **Primary arbiter** | Once ≥100 closed trades, live evidence outranks all backtests. Live A/B experiments do not consume backtest budget. |

## 2. Multiple-comparisons policy

- Exploratory results are reported with the running count of experiments-to-date on that
  data (currently: 9 on the worn window).
- A variant graduates to a confirmatory holdout look only if its exploratory effect is
  LARGE (≥ +0.10 PF or ≥ +$2.00/trade vs baseline) — small exploratory wins are noise by
  presumption.
- Confirmatory acceptance uses the pre-registered threshold below, evaluated ONCE. A
  failed confirmatory look kills the variant permanently (no re-tries with tweaks).
- **HARKing is banned**: hypotheses are registered in this file (with thresholds and
  minimum n) BEFORE the data that judges them is examined. Git history is the timestamp.

## 3. Pre-registered hypotheses (registered 2026-07-05, before any judging data existed)

### H1 — IV/RV premium gate (live data)
- **Hypothesis:** trades entered with `iv_short > 1.2 × rv_60m` have higher expectancy
  than trades with `iv_short ≤ 1.2 × rv_60m`.
- **Test:** difference in mean $/trade; bootstrap 95% CI.
- **Accept:** CI lower bound > $0. **Minimum n:** 60 trades per group.
- **Data:** TradeLog columns `iv_short`, `rv_60m` (recorded on every entry since 2026-07-05).

### H2 — $10-width structural change (confirmatory holdout look #1)
- **Context:** exploratory result on the worn window: PF 1.02, +$0.52/trade (vs 0.94,
  −$1.04 at $5 width) — the first PF > 1.0 in system history. Exploratory ≠ real.
- **Hypothesis:** $10-width spreads, otherwise-identical baseline config, achieve **PF >
  1.05** on the reserved holdout with modeled slippage + commissions.
- **Test:** one run of `research/spreads.py --width 10` over the holdout dates. ONE look.
- **Accept:** PF > 1.05. Reject → $10-width is dead historically (live A/B may still speak).
- **Scheduled:** after ≥1 month of live width A/B (so both evidence streams mature together).

### H2b — live width A/B (running from 2026-07-06)
- **Mechanism:** per-entry width ∈ {5, 10} by deterministic md5 hash of the entry minute
  (`intelligence.width_experiment_enabled`). Unbiased, reproducible from the TradeLog.
- **Hypothesis:** $10-width trades have ≥ $1.00/trade higher expectancy than $5-width.
- **Accept:** bootstrap 95% CI lower bound > $0 at **n ≥ 50 per arm**.
- **Kill:** if the $10 arm's max drawdown exceeds 2× the $5 arm's at any n ≥ 30, pause the
  experiment and review gap-through tail risk before continuing.

### H3 — limit-first exits (running from 2026-07-06)
- **Hypothesis:** limit-first closes achieve better fills than market closes:
  mean `exit_slippage` (fill − estimate) for limit exits < mean for market exits.
- **Accept:** limit-exit mean slippage ≤ market-exit mean − $0.02, or at minimum not worse
  by more than $0.05 (else disable `limit_exits`). **Minimum n:** 50 limit exits.
- **Data:** TradeLog `exit_slippage`, `limit_exit`.

### H4 — profit-target A/B (QUEUED — do not start yet)
- **Design:** PT ∈ {40%, 60%} by the same deterministic hash; two arms only (three is
  underpowered below n≈200).
- **Start condition:** after H2b concludes (avoid a 2×2 factorial at 2–4 trades/day).
- **Accept:** an arm wins if its $/trade 95% CI excludes the other arm's mean at n ≥ 50
  per arm; otherwise PT stays 50%.

## 4. Standing rules

1. **Signals freeze:** no new entry-signal models until the TradeLog itself provides
   evidence that entries (not costs) are the binding constraint.
2. **Instrument, don't adopt:** proposals needing data we lack specify the TradeLog
   columns to add, and wait.
3. **Only-if-better + sanity floor:** every nightly model promotion must beat the deployed
   model on holdout AND be within 2× the median of recent deployed metrics
   (`models/metrics_history.json`); corrupted-data retrains abort on a bar-count guard.
4. **Every experiment gets a row in `docs/AI_REVIEW.md`** — including failures. Especially
   failures.
5. **Promotion to real money** remains governed by SYSTEM.md §12 (unchanged by anything
   in this protocol).

## 5. Current live experiment register

| Experiment | Arms | Assignment | Started | Decision point |
|---|---|---|---|---|
| H2b width | $5 / $10 | md5(entry minute) | 2026-07-06 | n ≥ 50/arm |
| H3 limit exits | limit-first vs market (natural split by urgency) | exit-type | 2026-07-06 | n ≥ 50 limit exits |
| H1 IV/RV | observational (no assignment) | — | 2026-07-06 | n ≥ 60/group |
| H4 profit target | 40% / 60% | md5(entry minute) | **not started** | after H2b |
