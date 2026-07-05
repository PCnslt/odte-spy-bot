# Architecture

## Design principles

1. **Real data everywhere; only fills are paper.** Backtests use real Polygon SPY + 0DTE
   option bars. The live loop uses real-time IBKR quotes. Orders route to an IBKR paper account.
   There is no Black–Scholes and no modeled option price in any runtime path.
2. **One interface per boundary.** Data source, broker, and model each sit behind a small class
   so you can swap Polygon→another vendor or paper→live without touching strategy code.
3. **The backtest must not lie.** Entries/exits happen at real option bars; slippage and
   commission are applied on top (fills are the only unavoidable modeling in any backtest).
4. **Fail closed.** Any anomaly, missing data, or risk breach halts trading rather than guessing.
5. **Never fabricate an input.** If a real series (e.g. VIX) isn't entitled, we drop it and
   train/trade without it — we do not substitute a made-up value.

## Layers

### `src/common.py`
Shared enums (`Signal`, `Regime`, `OrderSide`) and dataclasses (`MarketSnapshot`, `TradeIntent`,
`Fill`, `Position`, `TradeResult`). Everything else imports from here so types stay consistent.

### `src/data/`
- `polygon_options.py` — `PolygonOptions`: real SPY history, real 0DTE option-contract chains,
  and per-contract minute bars from Polygon.io. Parquet-cached under `data/`.
- `ibkr_feed.py` — real-time SPY bars + real option premium/ATR via `ib_insync` (TWS/Gateway),
  for the live loop. Drops VIX rather than faking it if unavailable.
- `data_pipeline.py` — CLI to pull/cache real bars and assemble the training frame. Also builds
  a `MarketSnapshot` from the newest bars. `include_vix` follows whether real VIX is present.

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
- `ibkr_broker.py` — `IBKRBroker`: native bracket orders via `ib_insync`, against the IBKR
  paper account (default) or a gated live account. The only broker; there is no sim broker.
- `risk.py` — stop/target computed on the **real option premium** and the option's own ATR
  (no delta/BS), position sizing (% of equity), daily-loss halt.
- `position_manager.py` — turns a `Signal` + real option inputs into a sized `TradeIntent`
  and enforces daily guardrails.

The backtester's fill loop lives in `src/backtest.py` and walks real option bars directly —
there is no separate simulated broker.

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
Event loop over real SPY bars. For each entry signal it resolves the actual listed ATM 0DTE
contract for that day (Polygon chain), enters at its **real** minute-bar price, and walks the
contract's **real** minute bars until SL / TP / time-stop / session flatten — applying slippage
+ commission. For a single concurrent position it fast-forwards the main loop to the exit bar.
Emits a report. No modeled prices.

### `src/main.py`
The live/paper loop: poll data → snapshot → signal → risk gate → broker → record → monitor.
Runs a scheduler so it only trades in RTH and flattens before close.

## Data flow of one trade

```
bar tick ─▶ MarketSnapshot ─▶ features ─▶ ml_prob
                          └─▶ regime, rules
   all of the above ─▶ SignalGenerator ─▶ Signal.BUY_CALL
   Signal ─▶ resolve REAL 0DTE contract (Polygon chain / IBKR) ─▶ real premium + option ATR
   ─▶ PositionManager (size, SL, TP on real premium, daily-halt) ─▶ TradeIntent
   TradeIntent ─▶ backtest: walk real option bars │ live: IBKRBroker.place_bracket
   ─▶ TradeResult ─▶ evaluator ─▶ (maybe) self_corrector / retrain flag
```

## Known limitations (documented, not hidden)

- **Fills are simulated against real prices.** In the backtest we assume a stop fills at the
  stop price and a target at the target price, plus a flat slippage fraction. Real 0DTE fills
  can be worse on fast moves. Backtest numbers are an optimistic-but-real-data estimate, not a
  guarantee; the live paper phase is the real test.
- **Data cost is real.** Backtests need a Polygon Options plan; the live loop needs IBKR
  real-time entitlements. VIX features need a Polygon Indices entitlement (else dropped).
- Slippage is a flat fraction, not order-book/queue aware.
