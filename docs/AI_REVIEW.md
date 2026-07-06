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
