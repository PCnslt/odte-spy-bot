# SYSTEM.md — Complete System Reference

*Snapshot: 2026-07-05. The authoritative what/why/how of the odte-spy-bot system as deployed.*

---

## 1. What this system is

A **fully automated 0DTE SPY options paper-trading system** that sells defined-risk credit
spreads on Interactive Brokers' paper account, driven by real market data end-to-end, with a
nightly self-retraining ML component and an honest research harness that decides what is
allowed to be traded.

**Core philosophy — three rules that shaped every decision:**
1. **Real everything except the money.** Real historical data (Polygon), real-time quotes
   (IBKR), real order routing (IBKR paper). The only simulated thing is the account balance.
   No Black–Scholes, no synthetic prices, no faked inputs (e.g., VIX is *dropped*, not
   defaulted, when not entitled).
2. **Out-of-sample or it didn't happen.** Every strategy claim must survive walk-forward
   testing on data the model never saw. The system's own research killed its first strategy.
3. **Fail closed.** Missing data, anomalies, risk breaches, unreachable Gateway → halt/skip,
   never guess.

---

## 2. Infrastructure map

```
┌─ This Mac (America/New_York) ─────────────────────────────────────────┐
│  launchd (com.pcnslt.odte-spy-bot)     pmset wake 9:20 weekdays       │
│    └─ scripts/run_paper_day.sh  (9:25 ET weekdays)                    │
│         ├─ git pull --ff-only         ← picks up nightly model        │
│         ├─ port-check 127.0.0.1:4002  ← fails loudly if Gateway down  │
│         └─ caffeinate python -m src.main --mode paper --daily         │
│                                                                       │
│  IB Gateway 10.45 (/Applications), logged into PAPER (DUR193467)      │
│    • API port 4002, Read-Only API disabled, auto-restart 23:45        │
└───────────────┬───────────────────────────────────────────────────────┘
                │ real-time bars, contract qualification, combo orders
                ▼
        IBKR paper account (DUR193467, $1,000,000 virtual, real routing)

┌─ Cloud ───────────────────────────────────────────────────────────────┐
│  Polygon/Massive  Options Starter $29/mo (paid 2026-07-05)            │
│    • REST aggregates: SPY + all option contracts, ~2yr history        │
│    • greeks/IV/OI snapshot, flat files, websockets (entitled)         │
│    • NOT entitled: NBBO quotes (Advanced-only), indices/VIX           │
│                                                                       │
│  GitHub (private) PCnslt/odte-spy-bot                                 │
│    • tests.yml — CI on every push (23 tests)                          │
│    • daily_retrain.yml — 21:00 UTC weeknights: pull real data,        │
│      retrain, commit model ONLY if validation improves                │
│    • POLYGON_API_KEY stored as encrypted Actions secret               │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 3. Codebase architecture

```
src/
├── common.py          Shared types: Signal, Regime, MarketSnapshot, TradeIntent,
│                      TradeResult, ExitReason. Single source of truth for dataclasses.
├── main.py            LIVE LOOP (credit spreads) + --selftest + --daily + live gating
├── backtest.py        Single-leg backtester on real Polygon option bars (research)
├── data/
│   ├── polygon_options.py  Polygon REST client: chains, option/stock minute bars,
│   │                       parquet cache, 429 backoff + self-throttle, ticker builder
│   ├── ibkr_feed.py        Real-time SPY bars, option resolution, SPREAD resolution
│   │                       (qualify legs, real credit estimate, close-cost pricing)
│   └── data_pipeline.py    Download/cache real bars, training-set assembly, snapshots
├── signals/
│   ├── feature_engineering.py  19 causal features (21 w/ VIX); no look-ahead
│   ├── labeling.py             Magnitude-aware directional labels (triple-barrier-lite)
│   ├── lightgbm_model.py       DirectionalClassifier: walk-forward split, persistence
│   ├── regime_classifier.py    Transparent threshold regime tags (no black box)
│   ├── sentiment_analyzer.py   Optional FinBERT (off by default; degrades to neutral)
│   └── signal_generator.py     Fuses rules + ML + regime (+ sentiment veto) → Signal
├── execution/
│   ├── broker_base.py       Broker ABC
│   ├── ibkr_broker.py       IBKR orders: brackets (legacy) + CREDIT-SPREAD combos (BAG)
│   ├── risk.py              Premium-based stops/targets + %-equity sizing
│   └── position_manager.py  Daily guardrails: max trades, loss halt, concurrency
├── learning/
│   ├── trainer.py           Train/retrain CLI; retrain replaces model only if better
│   ├── evaluator.py         Win rate, PF, Sharpe, maxDD, expectancy; retrain trigger
│   ├── anomaly_detector.py  Price-shock/staleness/latency → REDUCE_RISK or HALT
│   └── self_corrector.py    Bounded, audited parameter nudges (governor, not optimizer)
├── research/
│   ├── walkforward.py       Rolling train/test OOS harness (single-leg)
│   └── spreads.py           CREDIT-SPREAD backtester + walk-forward (real legs)
└── utils/                   config (3 YAMLs + .env), JSON logging, SQLite memory
                             (time-gate + whipsaw guards), Telegram alerts (optional)
```

Config: `config/config.yaml` (session, signal, spread, IBKR ports), `risk_params.yaml`
(sizing/limits/commissions), `model_params.yaml` (labels/LightGBM). Secrets in `.env`
(gitignored). Everything testable: **23 pytest tests**, run in CI on every push.

---

## 4. The signal logic (when it trades)

Per 30-second poll during 09:30–15:30 ET:

1. **Features** (causal, from real IBKR minute bars): returns (1/5/15m), session VWAP
   deviation, relative volume + volume z-score, ATR(5/15), realized vol (5m + annualized),
   RSI(14), MACD (+signal/hist), EMA 9/21 spread & slope, distance to 5-min high/low,
   minutes into session. VIX features only if a real VIX series exists.
2. **ML probability**: LightGBM binary classifier → P(SPY up ≥ +0.20% before −0.20%,
   within 10 minutes). Trained nightly on the trailing 30 days; walk-forward validated;
   the retrainer keeps the old model unless the new one scores better on holdout.
2b. **Range forecaster (the spread-seller's real target)**: LightGBM *regression*
   predicting the forward 60-minute maximum excursion of SPY (fraction of spot). Drives
   strike placement and entry skips. Retrained nightly with an only-if-better MAE gate.
   Fallback when absent: ATR-scaled √time estimate. (Rationale + rejected alternatives:
   `docs/AI_REVIEW.md` — direction has almost no learnable structure, range does.)
3. **Regime tag**: VOLATILE / TREND_UP / TREND_DOWN / CHOP from ATR + EMA slope + realized
   vol thresholds (transparent, inspectable).
4. **Decision** (`SignalGenerator`):
   - RVOL must exceed `min_rvol` (1.2).
   - Price above VWAP band (+0.1%) → long bias; below → short bias. Optional 5-min
     breakout confirmation (`require_breakout`).
   - ML must agree: P(up) ≥ 0.55 for bullish, ≤ 0.45 for bearish. *(Calibrated to the
     model's real probability spread — retune after retrains; a threshold above the
     model's max = zero trades.)*
   - VOLATILE regime demands extra conviction; optional FinBERT bearish veto blocks longs.
5. **Consistency memory** (SQLite): 3-minute time gate between decisions; max 2 bias flips
   per rolling hour (whipsaw guard).

## 5. The strategy (what it trades) — 0DTE credit spreads

**Why selling, not buying:** the system's own research (Section 7) proved buying 0DTE
premium is a structural loser (theta + spread), while selling defined-risk verticals was
near break-even *before commissions* — the only variant with a plausible path to positive.

**Construction (all real, resolved live from IBKR):**
- Bullish signal → **bull put spread** (short put below spot, long $5 lower);
  bearish → **bear call spread** (short call above, long $5 higher).
- **Strike distance**: static 0.2% OTM by default. Range-forecast placement
  (`intelligence.use_range_strikes`) is implemented and OOS-tested but ships **OFF** —
  the 90-day decomposition showed it trims drawdown yet costs expectancy in calm regimes
  (PF 0.94→0.84). The forecast is still computed and logged on every entry.
- **Event guard** (`config/events.yaml`): FOMC/CPI days block entries or double the
  safety margin.
- **Liquidity gate**: real leg bid/asks fetched; if the legs' half-spreads sum to >25% of
  the credit, skip — transaction costs would eat the trade.
- Skip if net credit < $0.10.
- **Atomic combo (BAG) order** — both legs in one order, no leg risk. Entry limit at 95%
  of the quote-mid credit (or 90% of last-price estimate when quotes unavailable).
  Unfilled entries auto-cancel after 3 minutes.

**Sizing:** risk 2% of account equity against max loss = (width − credit) × 100 per
contract; hard cap 5 contracts; minimum 1.

**Exits (managed on real leg prices every poll):**
| Trigger | Rule |
|---|---|
| Profit target | Close when buy-back cost ≤ 50% of credit received |
| Strike defense (**default OFF**) | Close when spot comes within 0.1% of the short strike. OOS decomposition: whipsaws badly near static strikes (PF 0.50) — enable only with range-placed strikes (`intelligence.defense_enabled`) |
| Stop loss | Close when cost ≥ 2× credit (capped at spread width) |
| Time stop | Close after 240 minutes (theta needs hours, not 10 minutes) |
| Session flatten | Everything closed at 15:55 ET, no exceptions |
| Anomaly halt | 3σ 1-min price shock → flatten + pause |
Closes are market combo orders (guaranteed exit; fill-quality measurement is a goal of
the paper phase).

**Daily guardrails:** max 4 trades/day; max 1 concurrent position; hard halt for the day
at −2% account P&L; no new entries after 15:30.

## 6. Risk & safety systems

- **Anomaly detector**: 3σ one-minute return → HALT (flatten + pause); realized-vol spikes
  → reduce-risk; stale data (>120s) → HALT; slow execution (>2s) → reduce-risk.
- **Self-corrector** (bounded governor): losing streak (win<40% over ≥20 trades) → cut
  risk 20% + raise conviction threshold; hot streak → +10% risk. Every knob is clamped
  (risk 0.5–3%, thresholds 0.55–0.75, stop width 0.5–1.5×) and every change is logged.
  It can never re-risk its way out of a drawdown.
- **Live-money gate**: real-money mode requires BOTH `--mode live` AND
  `execution.live_confirmed: true` in config. Defaults refuse.
- **Ops fail-safes**: runner script aborts loudly if Gateway port is down; `--daily` exits
  only when flat; Gateway auto-restarts nightly; weekend guard.

## 7. Research record (the honest history — do not forget this)

All results are **walk-forward out-of-sample on real option prices**, fills crossing the
spread + $0.65/contract commissions:

| Strategy variant | OOS trades | Win | PF | $/trade | Verdict |
|---|---|---|---|---|---|
| Long premium (buy calls/puts) | 292 | 33.9% | 0.58 | −$32.95 | **Structural loser. Removed.** |
| Credit spreads, 10-min stop | 134 | 40.3% | 0.54 | −$5.26 | Wrong exit design |
| **Credit spreads, 240-min theta hold** | **107** | **63.6%** | **0.94** | **−$1.04** | **Near break-even; gross-positive before commissions** |

Interpretation: the sell side is structurally right; the residual loss ≈ transaction
costs (4 legs × $0.65 + crossing spreads). **The open question is fill quality**, which
is exactly what the live paper phase measures with real IBKR combo fills at the spread's
own bid/ask. Bugs found & fixed by this research: 5-min breakout including the current
bar; parquet µs-vs-ns index mismatch silently killing all but one trade; ML thresholds
set above the model's reachable maximum.

## 8. Self-learning loop

1. **Nightly (21:00 UTC weeknights, GitHub Actions):** pull 30 days of real data →
   retrain LightGBM → compare holdout logloss vs the deployed model → commit the new
   model **only if better** (verified working: `auto-retrain 2026-07-05`).
2. **Next morning 9:25:** runner `git pull`s the improved model before trading.
3. **Intraday:** performance monitor tracks rolling win rate; `should_retrain()` flags
   degradation (<40% over 50 trades); self-corrector applies bounded de-risking
   immediately without waiting for the nightly cycle.

## 9. Operations calendar

| When (ET) | What | Actor |
|---|---|---|
| 9:20 weekdays | Mac wakes (`pmset`) | macOS |
| 9:25 weekdays | Session runner starts (pull → check → trade) | launchd |
| 9:30–15:30 | Entries allowed, positions managed every 30 s | bot |
| 15:55 | Flatten everything | bot |
| ~16:00 | Bot exits for the day (`--daily`) | bot |
| 21:00 UTC weeknights | Cloud retrain → model commit if better | GitHub Actions |
| 23:45 nightly | Gateway auto-restart | IB Gateway |
| ~Sundays | **Manual: IBKR 2FA re-login to Gateway** | **you** |

**Monitoring:** `logs/daily_YYYYMMDD.log` (session narrative), `logs/bot.jsonl`
(structured), `logs/launchd.{out,err}`, GitHub Actions tab (retrains), Telegram alerts if
configured in `.env`. Trades/decisions persist in `memory.db` (SQLite).

**Kill switch:** `launchctl bootout gui/$UID ~/Library/LaunchAgents/com.pcnslt.odte-spy-bot.plist`
stops the schedule; Ctrl-C on a running session flattens before exiting; quitting Gateway
severs the API (bot fails safe next poll).

## 10. Costs

- Polygon/Massive **Options Starter: $29/mo** (Visa on file; next invoice Aug 5, 2026).
- IBKR paper account + Gateway: free. Commissions modeled at $0.65/contract/leg.
- GitHub Actions: within free tier. Total: **$29/mo**.

## 11. Known limitations (open-eyed)

1. **Fill realism is the experiment.** Research fills used last-traded aggregate prices ±
   slippage, not NBBO. Historical NBBO needs Polygon Advanced ($199). The paper phase
   answers this with real fills instead.
2. **IBKR market data may be delayed** (feed requests delayed-data fallback, type 3). For
   a minute-bar strategy with wide gates this is tolerable in paper; a real-time
   subscription (~$1.50–15/mo) would remove it.
3. **The ML edge is weak** — P(up) lives in ≈0.38–0.60. It's a filter, not an oracle.
   The strategy's economics come from theta + risk management, not prediction.
4. Single concurrent position; spread credit estimated from last leg prices (limit order
   protects entry); one strategy, one underlying.
5. Mac must be on (wake schedule handles sleep, not shutdown); IBKR 2FA weekly.

## 12. Promotion criteria (before any real dollar)

Do not enable live mode until ALL hold:
- [ ] ≥ 3 months of automated paper trading, ≥ 100 completed spreads
- [ ] OOS-consistent: paper profit factor > 1.15 after all costs
- [ ] Max drawdown within modeled bounds; no unexplained fills or rejections
- [ ] Anomaly halts and daily halts observed working in the wild
- [ ] You have read `src/execution/ibkr_broker.py` and accept every risk parameter
Then, and only then: fund the account you're willing to lose, set
`execution.live_confirmed: true`, run `--mode live`. **Nothing here is financial advice;
0DTE spreads can lose their full max-loss per trade.**
