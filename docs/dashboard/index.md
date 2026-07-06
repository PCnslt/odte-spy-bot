# odte-spy-bot — Live Dashboard

*Generated 2026-07-05 23:04 ET · auto-updated at each session close · **LIVE pre-registered evidence only** — historical exploration lives in [AI_REVIEW.md](../AI_REVIEW.md) and is never mixed into this page.*

> Context: the historical harness says this strategy is NEGATIVE under pessimistic fill assumptions (~−$8.6/trade). The open question this page answers over time: **do real fills beat that model?**

## Live results (paper)

_No closed trades yet. Zero trades on a given day is normal — the entry gates are strict._

## Fill quality — the decisive evidence

| Metric | n | Mean | Verdict (protocol thresholds) |
|---|---|---|---|
| Entry slippage (est − fill) | 0 | — | no data |
| Exit slippage — limit | 0 | — | no data |
| Exit slippage — market | 0 | — | no data |

## Pre-registered experiments — progress toward decision n

| Hypothesis | Groups (n so far) | Decision at |
|---|---|---|
| H2b width A/B | $5: 0 · $10: 0 | ≥50/arm |
| H1 IV/RV | IV>1.2×RV: 0 · rest: 0 | ≥60/group |
| H3 limit-vs-market exits | limit: 0 · market: 0 | ≥50 limit |
| H7 GEX regime | GEX+: 0 · GEX−: 0 | ≥60/group |
| H4 profit target | queued (starts after H2b) | — |

## Standing kill rule (adopted R11)

At n ≥ 500 live trades: if the bootstrapped 95% CI upper bound of $/trade is below $0, the strategy family is retired — hard-coded commitment, no appeals to 'one more tweak'.

---
*Sources: `trades.db` (TradeLog) · protocol: [RESEARCH_PROTOCOL.md](../RESEARCH_PROTOCOL.md) · full research record: [AI_REVIEW.md](../AI_REVIEW.md) · system reference: [SYSTEM.md](../../SYSTEM.md)*