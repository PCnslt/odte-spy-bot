# odte-spy-bot

A modular, self-monitoring research system for **0DTE SPY options** strategy development,
backtesting, and paper trading.

> ⚠️ **Read this first.** Trading 0DTE options can lose 100% of the capital in a position in
> minutes. This repository is an **educational / research** project. It uses **real market
> data** but routes orders to an **IBKR paper account by default** — it will not touch a
> real-money account unless you deliberately enable live execution (`--mode live` **and**
> `execution.live_confirmed: true`). No strategy here is guaranteed to be profitable. You are
> solely responsible for any losses.

---

## What this actually is (and isn't)

**It is:**
- A clean, testable Python codebase: data → features → signal → risk → execution → learning.
- A **real-data backtester** that fills against **actual historical 0DTE option bars from
  Polygon.io** — no Black–Scholes, no modeled prices.
- A **live paper-trading loop** driven by **real-time IBKR quotes**, routing orders to an
  **IBKR paper account** (real routing, virtual money).
- A monitoring / self-correction layer that tracks live performance and pulls the brakes when
  the strategy degrades.

**It is not:**
- A money printer. There is no "wins all the time." The design target is *measurable positive
  expectancy*, and the backtest is deliberately built to be pessimistic, not flattering.
- A high-frequency system. Data is minute-resolution; this is a scalping/intraday system, not HFT.

## What's real vs. simulated

**Everything is real except the fills, which are paper.** That is the whole design constraint.

| Component | Source |
| --- | --- |
| Backtest SPY + 0DTE option bars | **Real** — Polygon.io historical aggregates (actual traded prices). |
| Backtest fills | Simulated *against real prices* — entry/exit at real option bars + slippage/commission. |
| Live SPY + option quotes/greeks | **Real** — IBKR real-time market data (TWS / IB Gateway). |
| Live order routing | **Real** — sent to an **IBKR paper account** (real routing, virtual money). |
| Memory | SQLite (no Redis server). |
| Sentiment (optional) | Real FinBERT over real headlines; off by default. |

There is **no Black–Scholes and no modeled option pricing anywhere** in the backtest or live
paths. The only synthetic data in the repo lives in `tests/` (deterministic unit-test fixtures).

## Requirements

- **Polygon.io** Options plan with historical option aggregates (Options Starter or higher) —
  set `POLYGON_API_KEY`. Required for backtests.
- **Interactive Brokers** account + **TWS or IB Gateway** running with the API enabled, and a
  real-time market-data subscription covering SPY/options — required for the live loop.
| "Self-learning LLM sentiment" | Optional, lazy-loaded FinBERT. Off by default so the repo runs offline. |

## Architecture

```
data ─▶ features ─▶ signal (rules + LightGBM + regime + sentiment) ─▶ risk/sizing ─▶ broker
  │                                                                                     │
  └──────────────────────────── learning: monitor · anomaly · self-correct ◀───────────┘
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full breakdown.

## Quick start

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                     # set POLYGON_API_KEY (required for backtests)

# 1) Pull REAL SPY + 0DTE option history from Polygon and build the training set
python -m src.data.data_pipeline --download --days 30

# 2) Train the directional model on real data
python -m src.learning.trainer --train

# 3) Backtest with real historical option fills (no modeled prices)
python -m src.backtest --days 30

# 4) Live paper loop: real-time IBKR quotes -> IBKR paper account
#    (start TWS/IB Gateway in paper mode with the API enabled first)
python -m src.main --mode paper
```

## Research: is there an edge? (walk-forward, out-of-sample)

A single backtest overfits. The real test trains on a trailing window and trades the *next*
unseen window, rolling forward:

```bash
python -m src.data.data_pipeline --download --days 180
python -m src.research.walkforward --days 180 --train 20 --test 5
```

**Honest result as of this build: the strategy has NO EDGE, and we proved it.**

- Selective variant (breakout required): 18 OOS trades, 38.9% win, PF 0.77, ~-3%.
- Higher-frequency variant (`--no-breakout --quantile 0.15`, magnitude-aware labels):
  **292 OOS trades, 33.9% win, PF 0.58, -$9,621 on $10k, Sharpe -4.07, 20 of 21 folds negative.**

Buying 0DTE premium on this signal is a **structural loser** — you pay theta + spread every
trade and must be right *and* fast. The only winning fold was a big-move week; the strategy is
really a long-volatility bet that bleeds in calm markets. **Do not trade it.** The walk-forward
harness exists precisely so any future idea is judged out-of-sample, not curve-fit into a lie.

### Credit spreads (premium selling) — near break-even, costs are the frontier

`python -m src.research.spreads --days 90 --no-breakout --quantile 0.15` sells defined-risk
verticals (bull put / bear call, real legs from the real chain) instead of buying premium:

| Variant | OOS trades | Win | PF | Expectancy |
| --- | --- | --- | --- | --- |
| Long premium | 292 | 33.9% | 0.58 | -$32.95 |
| Spreads, 10-min stop (wrong exit) | 134 | 40.3% | 0.54 | -$5.26 |
| Spreads, theta hold (240 min) | 107 | **63.6%** | **0.94** | **-$1.04** |

Selling is confirmed as the right side. The residual loss ≈ transaction costs (4 legs ×
$0.65 commission + crossing the spread on each leg); gross expectancy before commissions is
slightly positive. The frontier is **fill quality**, not signal: NBBO-midpoint fills (needs
Polygon Developer quotes) and real IBKR spread-order paper fills are the next honest tests.

### Intelligence layer (range forecasting) — tested, partially adopted

A range-forecasting upgrade (predict the forward 60-min max excursion; place strikes beyond
it; defend the short strike) was OOS-decomposed before shipping:

| Variant | Trades | Win | PF | $/trade |
| --- | --- | --- | --- | --- |
| Baseline | 107 | 63.6% | **0.94** | **-$1.04** |
| + range strikes | 105 | 62.9% | 0.84 | -$2.17 |
| + defense exit | 124 | 44.4% | 0.50 | -$10.58 |
| + both | 106 | 61.3% | 0.77 | -$3.25 |

**Adopted (ON):** liquidity gate on real leg quotes, quote-mid entry pricing, event-day
guard, nightly range-model training (telemetry on every entry). **Implemented but default
OFF** (they degraded OOS expectancy): `intelligence.use_range_strikes`,
`intelligence.defense_enabled`. Full analysis + rejected LLM/transformer proposals:
[docs/AI_REVIEW.md](docs/AI_REVIEW.md).

## Data plan notes (what this Polygon plan actually allows)

- ✅ Historical SPY + option **aggregates** (minute bars) via REST, ~**2 years** back.
- ❌ **NBBO quotes** (needs Options Developer), ❌ **flat-file downloads** (add-on),
  ❌ **VIX / indices** (Indices add-on). The code detects these and degrades honestly
  (drops VIX, uses aggregate fills) rather than faking anything.
- The plan **rate-limits**; `data.polygon.rate_limit_per_min` throttles the client and it
  backs off on HTTP 429. All fetched bars are cached under `data/` so re-runs are fast.

## Live loop & extras

`ib_insync` is required for the live loop; install the extras:

```bash
pip install -r requirements-extras.txt   # ib_insync (IBKR), transformers+torch (sentiment)
```

- **IBKR paper (default live mode):** start TWS/IB Gateway in **paper** mode, enable the API,
  then `python -m src.main --mode paper`. Ports live in `config/config.yaml` (`execution.ibkr`).
- **IBKR live (real money):** requires `--mode live` **and** `execution.live_confirmed: true`.
  Read `src/execution/ibkr_broker.py` first.
- **Sentiment:** set `sentiment.enabled: true` in `config/config.yaml`.

## Safety model

1. Default is `--mode paper` → IBKR **paper** account. Real-money orders require `--mode live`
   **and** `execution.live_confirmed: true` in config.
2. Hard daily loss halt, max trades/day, and a time-stop on every position.
3. Anomaly detector flattens positions and pauses on price/volatility/latency shocks.
4. Everything is logged to `logs/` and to the SQLite trade memory.

## Before risking a cent

- [ ] Backtest expectancy is positive **and** you understand *why*.
- [ ] ≥ 3 months of paper trading, ≥ 100 trades.
- [ ] You've read `src/execution/` and understand exactly what live mode does.
- [ ] Risk parameters in `config/risk_params.yaml` are ones you can afford to lose.

## License

MIT — see [LICENSE](LICENSE). No warranty. Not financial advice.
