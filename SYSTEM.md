# SYSTEM.md — Complete System Reference

*Snapshot: 2026-07-05 (post rounds 1–3 of external AI review). The authoritative what/why/how
of the odte-spy-bot system as deployed. Companion: `docs/AI_REVIEW.md` — the full record of
every proposed upgrade, its out-of-sample test, and its disposition.*

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
│   ├── labeling.py             Directional labels + range labels (forward max excursion)
│   │                           + breach labels (P(adverse move ≥ strike distance))
│   ├── lightgbm_model.py       DirectionalClassifier: walk-forward split, persistence
│   ├── range_model.py          RangeForecaster (regression) + strike-placement helpers
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
│   (self_corrector.py removed in audit round 5 — dead code; the consecutive-loss
│    brake + nightly retraining cover its role)
├── research/
│   ├── walkforward.py       Rolling train/test OOS harness (single-leg)
│   └── spreads.py           CREDIT-SPREAD backtester + walk-forward (real legs);
│                            --smart (range strikes + defense) and --ev (EV gate) arms
└── utils/                   config (3 YAMLs + .env), JSON logging, SQLite memory
                             (time-gate + whipsaw guards), TradeLog (per-trade decision
                             context + fills → trades.db), EventGuard (events.yaml),
                             Telegram alerts (optional)
```

Config: `config/config.yaml` (session, signal, spread, intelligence flags, IBKR ports),
`risk_params.yaml` (sizing/limits/commissions/brakes), `model_params.yaml` (labels/LightGBM),
`events.yaml` (FOMC/CPI calendar, manually maintained). Secrets in `.env` (gitignored).
Everything testable: **35 pytest tests**, run in CI on every push.

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
   predicting the forward 60-minute maximum excursion of SPY (fraction of spot). Retrained
   nightly with an only-if-better MAE gate. Fallback when absent: ATR-scaled √time estimate.
   Notable: on identical features, the direction target fits in ~14 boosting rounds (nearly
   nothing to learn) vs ~119 for range — volatility is forecastable, direction mostly isn't.
   Currently used for **telemetry** (logged on every entry); strike placement from it is
   implemented but default-OFF (failed OOS — see §7).
2c. **Breach classifiers**: two LightGBM binary models, P(spot reaches the short strike
   within 120 min), down-side and up-side. Power the EV gate (default-OFF after failing
   OOS) and are logged per trade for future evaluation. Also retrained nightly, gated.
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

**Exit execution (limit-first):** non-urgent closes (profit target / time stop) place a
LIMIT combo at the current mid and escalate to market after 120 s unfilled; stops, defense,
and flatten are always market. Attacks the documented cost frontier; measured by the
TradeLog's est-vs-fill slippage columns on every close.

**Entry-quality gates (all fail-safe):** liquidity gate (sum of leg half-spreads ≤ 25% of
credit, from real quotes), event guard (`events.yaml`), EV gate (**default OFF** — see §7),
min-credit $0.10.

**Daily guardrails:** max 4 trades/day; max 1 concurrent position; hard halt for the day
at −2% account P&L; **consecutive-loss brake** (6 straight losses across days → pause
entries; resets on any win); no new entries after 15:30.

**Sizing reality check:** at $10k equity, 2% risk (~$200) is below one contract's max loss
(~$450) — **quantity is pinned at 1 on every trade**. Any sizing-based intelligence is a
no-op until equity grows ~5×; proposals in that space are rejected on arithmetic.

## 6. Risk & safety systems

- **Anomaly detector**: 3σ one-minute return → HALT (flatten + pause); realized-vol spikes
  → reduce-risk; stale data (>120s) → HALT; slow execution (>2s) → reduce-risk.
- **Consecutive-loss brake** replaces the former self-corrector (removed as dead code,
  audit round 5): 6 straight losses pause entries, reset on any win. Risk-reducing only.
- **Live-money gate**: real-money mode requires BOTH `--mode live` AND
  `execution.live_confirmed: true` in config. Defaults refuse.
- **Ops fail-safes**: runner script aborts loudly if Gateway port is down; `--daily` exits
  only when flat; Gateway auto-restarts nightly; weekend guard.

## 7. Research record — the complete gauntlet (do not forget this)

All results are **walk-forward out-of-sample on real option prices** (per-fold models see
only their training window), fills crossing the spread + $0.65/contract commissions.
Same 90-day window (62 trading days, 9 folds) for all spread variants.

| # | Variant | Trades | Win | PF | $/trade | Verdict |
|---|---|---|---|---|---|---|
| — | Long premium (buy calls/puts), 5 months | 292 | 33.9% | 0.58 | −$32.95 | **Structural loser. Removed from live.** |
| — | Credit spreads, 10-min scalper stop | 134 | 40.3% | 0.54 | −$5.26 | Wrong exit design for theta |
| 0 | **Credit spreads, 240-min theta hold (BASELINE)** | **107** | **63.6%** | **0.94** | **−$1.04** | **Best known config. Deployed.** |
| 1 | + range-forecast strike placement | 105 | 62.9% | 0.84 | −$2.17 | OFF — costs expectancy in calm regimes (does trim maxDD) |
| 2 | + strike-defense exit | 124 | 44.4% | 0.50 | −$10.58 | OFF — whipsaw machine near 0.2%-OTM strikes |
| 3 | + both (1+2) | 106 | 61.3% | 0.77 | −$3.25 | OFF |
| 4 | + EV gate v1 (width-based loss) | 0 | — | — | — | Bug-as-lesson: loss-given-breach under a 2× stop ≈ 1× credit, **not** width; blocked 100% of entries |
| 5 | + EV gate v2 (exit-structure EV) | 53 | 60.4% | 0.70 | −$5.69 | OFF — gate kept the WORSE half; breach probs don't rank trade quality on this window |

**Tally: 6 plausible strategy ideas tested (plus 2 rejected on arithmetic/data grounds
without testing), 0 survived.** Every surviving change is cost-avoidance mechanics:
liquidity gate, quote-mid entries, limit-first exits, event guard.

Interpretation: the sell side is structurally right; the residual loss ≈ transaction costs
(4 legs × $0.65 + crossing spreads); gross expectancy before commissions is slightly
positive. **The open question is fill quality**, which the live paper phase measures with
real IBKR combo fills. Bugs found & fixed by this research: 5-min breakout including the
current bar; parquet µs-vs-ns index mismatch silently killing all but one trade; ML
thresholds set above the model's reachable maximum; width-based EV mis-modeling
loss-given-breach.

**Round-5 fill-model correction (2026-07-05):** the audit fixed session-crossing labels,
stale-leg pricing, and polite stop fills. On the CORRECTED harness the same window reads:
$5 baseline **PF 0.70, −$6.62/trade (126 tr)**; $10 width **PF 0.90, −$2.30/trade (122 tr)**.
The table above is retained as the historical record of what each experiment was judged
against at the time; the corrected harness is now authoritative. Two conclusions survive
the correction: selling >> buying, and **wider width >> narrower** (+0.20 PF, robust across
fill models). The absolute "first PF>1.0" claim did not survive and is retracted. Reality
likely sits between the optimistic old harness and the deliberately pessimistic new one —
the TradeLog's est-vs-fill columns will locate it.

**Known validity caveat:** the same 90-day OOS window has now judged ~11 experiments. Its
p-values are eroding (multiple comparisons); treat further reuse skeptically. Fresh
evidence comes from (a) the live TradeLog and (b) extending history within the 2-year data
entitlement.

## 7b. Instrumentation doctrine (added round 3)

Proposals that need data the system doesn't have are neither accepted nor rejected — they
are **instrumented**. The TradeLog (`trades.db`) records, for every live trade: decision
context (regime, ML prob, range forecast, breach probabilities, short-leg IV from a live
Polygon snapshot, realized vol, RVOL/ATR/session-time) and execution truth (estimated vs
filled credit, estimated vs filled exit cost, limit-vs-market exit path, P&L). This is the
future training set for: IV/RV entry gating, fill-quality prediction, dynamic profit
targets, and regime clustering. `python -m src.utils.trade_log` reports with a hard
"n<30 = noise" warning.

## 8. Self-learning loop

1. **Nightly (21:00 UTC weeknights, GitHub Actions):** pull 30 days of real data → retrain
   **four models** (direction classifier, range forecaster, breach-down, breach-up) → each
   compared to the deployed version on holdout (logloss / MAE) → committed **only if
   better** (verified working: `auto-retrain 2026-07-05`).
2. **Next morning 9:25:** runner `git pull`s the improved models before trading.
3. **Intraday:** performance monitor tracks rolling win rate; consecutive-loss brake pauses
   entries after 6 straight losses.
4. **Continuously:** the TradeLog accumulates the decision-context + fill dataset that
   future upgrades will be trained and judged on.

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
configured in `.env`. Decision bias/whipsaw state in `memory.db`; per-trade context + fills
in `trades.db` (`python -m src.utils.trade_log` for the report).

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
   answers this with real fills instead — now instrumented per-trade in the TradeLog.
2. **The 90-day OOS window is wearing out.** ~8 experiments have been judged on the same
   62 trading days; multiple-comparisons risk is real. New evidence sources: live TradeLog,
   longer history within the 2-year entitlement, or a reserved never-touched holdout.
3. **IBKR market data may be delayed** (feed requests delayed-data fallback, type 3). For
   a minute-bar strategy with wide gates this is tolerable in paper; a real-time
   subscription (~$1.50–15/mo) would remove it.
4. **The directional ML edge is weak** — P(up) lives in ≈0.38–0.60. It's a filter, not an
   oracle. The strategy's economics come from theta + risk management, not prediction.
5. **No IV history, no NBBO history, no news feed** on current entitlements — any proposal
   requiring them is untestable today (instrument-don't-adopt doctrine applies).
6. Single concurrent position; qty pinned at 1 by account scale; one strategy, one
   underlying; spread credit estimated from last leg prices when quotes are unavailable.
7. Mac must be on (wake schedule handles sleep, not shutdown); IBKR 2FA weekly.

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
