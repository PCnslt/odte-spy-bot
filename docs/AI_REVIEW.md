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
