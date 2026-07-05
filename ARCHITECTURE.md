# Architecture

## Design principles

1. **Paper-first, offline-first.** The whole pipeline runs with no paid feeds and no live
   account. Live wiring is an opt-in adapter, not a dependency.
2. **One interface per boundary.** Data source, broker, and model are each behind a small
   abstract base class so you can swap `yfinance`→Polygon or `SimBroker`→`IBKRBroker`
   without touching strategy code.
3. **The backtest must not lie.** Slippage, commission, spread, and theta decay are all modeled.
   When we approximate (option prices from the underlying), we say so loudly.
4. **Fail closed.** Any anomaly, missing data, or risk breach halts trading rather than guessing.

## Layers

### `src/common.py`
Shared enums (`Signal`, `Regime`, `OrderSide`) and dataclasses (`MarketSnapshot`, `TradeIntent`,
`Fill`, `Position`, `TradeResult`). Everything else imports from here so types stay consistent.

### `src/data/`
- `free_feed.py` — `YFinanceFeed`: historical + latest SPY/VIX minute bars (free).
- `ibkr_feed.py` — optional live SPY/VIX quotes via `ib_insync` (TWS/Gateway).
- `data_pipeline.py` — CLI to download, cache (Parquet under `data/`), and assemble the
  training frame. Also builds a `MarketSnapshot` from the newest bars for the live loop.

### `src/signals/`
- `feature_engineering.py` — pure functions producing the feature matrix (price/vol/momentum/
  volatility/options-proxy features). No look-ahead: every feature at bar *t* uses only data
  ≤ *t*.
- `labeling.py` — forward-return triple-barrier-ish label: `1` if SPY is > `+τ%` within the
  horizon before hitting `-τ%`, else `0`.
- `lightgbm_model.py` — `DirectionalClassifier`. Walk-forward split, early stopping, model
  persisted to `models/`.
- `regime_classifier.py` — cheap, transparent regime tag (trend/chop/volatile) from ATR + EMA
  slope + VIX. No black box.
- `sentiment_analyzer.py` — optional FinBERT; returns 0.0 when disabled/unavailable.
- `signal_generator.py` — combines rules + ML prob + regime + sentiment + memory into a
  single `Signal`. This is the decision point.

### `src/execution/`
- `broker_base.py` — `Broker` ABC: `place_bracket`, `positions`, `flatten`, `account_value`.
- `sim_broker.py` — `SimBroker`: fills against modeled option prices, enforces SL/TP/time-stop.
  This is the default paper broker (offline, no TWS).
- `ibkr_broker.py` — `IBKRBroker`: native bracket orders via `ib_insync`. Works against the
  IBKR paper account or (gated) a live account.
- `risk.py` — position sizing (% of equity), volatility-adjusted SL/TP, daily loss halt.
- `pricing.py` — Black–Scholes price + greeks, used by both SimBroker and the backtester.

### `src/learning/`
- `evaluator.py` — rolling win rate, profit factor, Sharpe, max drawdown. `should_retrain()`.
- `anomaly_detector.py` — z-score price shocks, IV spikes, execution latency, data staleness.
- `self_corrector.py` — bounded parameter nudges (position size, ML threshold, SL width) with
  an audit trail. Never unbounded.
- `trainer.py` — CLI training/retraining entry point (used by CI too).

### `src/utils/`
- `config.py` — loads/merges the three YAMLs + `.env`, gives a typed `Config`.
- `logger.py` — structured JSON logging to file + console.
- `memory.py` — SQLite `TradingMemory`: current bias, decision log, consistency gate
  (time gate + whipsaw guard).
- `alerts.py` — Telegram alerts if configured, else no-op logging.

### `src/backtest.py`
Event loop over historical bars. For each entry signal it prices the synthetic 0DTE option
from the underlying (Black–Scholes with a term-structure-ish IV proxy), then walks the option
P&L bar-by-bar until SL / TP / time-stop, applying slippage + commission. Emits a report.

### `src/main.py`
The live/paper loop: poll data → snapshot → signal → risk gate → broker → record → monitor.
Runs a scheduler so it only trades in RTH and flattens before close.

## Data flow of one trade

```
bar tick ─▶ MarketSnapshot ─▶ features ─▶ ml_prob
                          └─▶ regime, sentiment, rules
   all of the above ─▶ SignalGenerator ─▶ Signal.BUY_CALL
   Signal ─▶ RiskManager (size, SL, TP, daily-halt check) ─▶ TradeIntent
   TradeIntent ─▶ Broker.place_bracket ─▶ Fill
   Fill ─▶ TradingMemory + PerformanceMonitor
   on close ─▶ TradeResult ─▶ evaluator ─▶ (maybe) self_corrector / retrain flag
```

## Known limitations (by design, documented not hidden)

- Option prices in sim/backtest are **modeled**, not real fills. Real 0DTE spreads and gamma
  make live results differ. Treat backtest numbers as an upper bound on skill, not a promise.
- yfinance minute history is ~30 days. For longer studies you need a paid feed; the `Feed`
  interface makes that a one-file change.
- No real Level 2 / order-book. Slippage is a flat model, not queue-position-aware.
