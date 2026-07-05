# odte-spy-bot

A modular, self-monitoring research system for **0DTE SPY options** strategy development,
backtesting, and paper trading.

> ⚠️ **Read this first.** Trading 0DTE options can lose 100% of the capital in a position in
> minutes. This repository is an **educational / research** project. It ships in **paper /
> simulation mode by default** and will not touch a real brokerage account unless you
> deliberately configure and enable live execution. No strategy here is guaranteed to be
> profitable. You are solely responsible for any losses.

---

## What this actually is (and isn't)

**It is:**
- A clean, testable Python codebase: data → features → signal → risk → execution → learning.
- An **honest backtester** that models 0DTE option P&L from the SPY underlying using
  Black–Scholes (because free tick-level 0DTE options history does not exist — see below).
- A **paper-trading loop** you can watch run today, with zero paid subscriptions.
- A monitoring / self-correction layer that tracks live performance and pulls the brakes when
  the strategy degrades.

**It is not:**
- A money printer. There is no "wins all the time." The design target is *measurable positive
  expectancy*, and the backtest is deliberately built to be pessimistic, not flattering.
- A high-frequency system. Robinhood and free data are minute-resolution at best.

## Honest constraints (please read)

| Thing the original spec assumed | Reality here |
| --- | --- |
| Polygon.io / ThetaData tick feeds | **Paid.** We use free `yfinance` minute bars instead. |
| Free historical 0DTE options chains | **Don't exist.** Backtest approximates option P&L from the underlying via Black–Scholes. |
| Redis for memory | Swapped for **SQLite** — no server to run. |
| IBKR `ib_insync` | Kept. Execution + optional live quotes go through TWS / IB Gateway. |
| "Just run the bot" | IBKR needs **TWS or IB Gateway running** with the API enabled. Offline work uses `SimBroker`. |
| Broker required for paper | Two options: offline `SimBroker` (default) **or** IBKR's real paper account (`--broker ibkr --mode paper`). |
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
pip install -r requirements.txt        # core only; extras are optional (see below)

cp .env.example .env                    # fill in what you have (nothing required for paper)

# 1) Pull free SPY minute data and build a training set
python -m src.data.data_pipeline --download --days 30

# 2) Train the directional model
python -m src.learning.trainer --train

# 3) Backtest on the downloaded data (honest option-P&L approximation)
python -m src.backtest --days 30

# 4) Run the paper-trading loop (simulated broker, no account needed)
python -m src.main --broker sim --mode paper
```

## Optional extras

```bash
pip install -r requirements-extras.txt   # transformers+torch (sentiment), ib_insync (IBKR)
```

- **Sentiment:** set `sentiment.enabled: true` in `config/config.yaml`.
- **IBKR paper:** start TWS/IB Gateway (paper), enable the API, then
  `python -m src.main --broker ibkr --mode paper`. Ports are in `config/config.yaml`.
- **IBKR live (real money):** requires `--broker ibkr --mode live` **and**
  `execution.live_confirmed: true` in config. Read `src/execution/ibkr_broker.py` first.

## Safety model

1. Default is `--broker sim --mode paper` (offline SimBroker). Real orders require
   `--broker ibkr --mode live` **and** `execution.live_confirmed: true` in config.
2. Hard daily loss halt, max trades/day, and a time-stop on every position.
3. Anomaly detector flattens positions and pauses on price/IV/latency shocks.
4. Everything is logged to `logs/` and to the SQLite trade memory.

## Before risking a cent

- [ ] Backtest expectancy is positive **and** you understand *why*.
- [ ] ≥ 3 months of paper trading, ≥ 100 trades.
- [ ] You've read `src/execution/` and understand exactly what live mode does.
- [ ] Risk parameters in `config/risk_params.yaml` are ones you can afford to lose.

## License

MIT — see [LICENSE](LICENSE). No warranty. Not financial advice.
