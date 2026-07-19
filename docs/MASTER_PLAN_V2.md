# 0DTE Master Plan v2 — Research Edition

**Prepared 2026-07-19 · for Shawn (PCnslt) · supersedes the v1 architecture packet**
All claims cited. Sources at the end of each section. Research window: live web, July 2026.

---

## 0. Verdict up front

**GO / NO-GO for live trading within one month: NO-GO.**

Not because the software isn't ready — because the *evidence* says the strategy family you're running has no demonstrated retail edge, and the one redesign that is defensible needs a realistic backtest and ~100 paper fills before it earns real money. Section 9 lists the exact conditions that flip this to GO.

The single most defensible statement the research supports:

> There is no credible, long-run, out-of-sample evidence that systematically selling 0DTE premium in retail structures (credit spreads, iron condors) is profitable net of realistic transaction costs and tail risk. The naive structures are break-even-to-**negative** net of costs, the residual edge has been **decaying since daily expirations arrived (May 2022)**, and the catastrophic tail (a 2008/2020-style day) has **never been sampled** by any 0DTE track record.

What *does* survive scrutiny is narrower: a small, real volatility risk premium at the 0-day horizon, harvestable only in specific structures (put ratio spreads / diversified short-premium baskets — **not** iron condors), only with disciplined VRP-conditional entry, tail-budgeted sizing, and a convexity hedge. Net Sharpe ≈ 0.8–0.9 in the best academic backtest — *before* the post-2022 decay, and with no crash in sample. That is the honest ceiling.

---

## 1. What the evidence actually says

### 1.1 Retail 0DTE traders lose money in aggregate — mostly to costs

| Finding | Number | Source |
|---|---|---|
| Aggregate retail 0DTE P&L | **−$241k/day**, growing to **−$350k/day** after daily expirations began | Beckmeyer, Branger & Gayda (SSRN 4404704) |
| Of >$70M measured retail losses | **>$50M was transaction costs** paid to market makers | same |
| Retail autotraders profitable | **49.6%** — a coin flip (230k trades) | OptionAlpha 0DTE study |
| Published option-Sharpe inflation from look-ahead data filters | **0.5 → 5+** | Duarte et al., *Rev. Financial Studies* (SSRN 4590083) |

The mechanism is not directional error. It is **fees and spread-crossing**. Any strategy you build must survive that arithmetic first.

### 1.2 Net-of-cost Sharpe by structure (10:00 ET entry, half-spread + slippage)

| Structure | Gross SR | **Net SR** |
|---|---|---|
| Put ratio spread | 1.18 | **0.93** |
| Diversified basket (top 3 structures) | 1.12 | **0.82** |
| Short straddle/strangle | 0.56 | **0.39** |
| **Iron condor / butterfly** (what retail trades) | 0.77 | **−0.20** |

Source: Vilkov 0DTE strategies (github.com/vilkovgr/0dte-strategies, companion to Dim–Eraker–Vilkov SSRN 4692190). Caveats that apply to *all* rows: sample is post-2022 only, the pricing-violation edge "dissipates after daily availability of 0DTEs" (Almeida–Freire–Hizmeri, SSRN 4701401), and no crash day exists in any 0DTE sample.

**Your current bot sells tight credit spreads — the structure family with the NEGATIVE net Sharpe.**

### 1.3 The tail is the whole story

- 1%-tail expected shortfall on short 0DTE premium: **0.58–1.58% of the underlying per event** — dwarfs the ~0.001% median daily VRP capture (Vilkov).
- A 0DTE iron condor typically risks **~$9 to make ~$1** → needs ~90% wins to break even before costs; real entry win rates run 65–70% (apexvol).
- The one profitable disclosed multi-year practitioner log (Theta Profits, 9,000+ SPX 0DTE trades) wins at **40%** — profitable only via 2.2× win/loss asymmetry and active intraday management, with double-stop days rising (8.6%) and premium capture decaying (5.65% → 3.53%).
- Practitioner consensus (EliteTrader, multi-year sellers): "**1–2 bad weeks give back all profits.**"
- Stops do not save you: on 0DTE, gamma can move a position **+100% → −100% in minutes**; a documented r/algotrading report measured stop execution lagging **up to 15 minutes** (25,000-trade study). Premium stops are not a real control at this horizon.
- Benn Eifert (QVR): "Systematically, blindly selling options is a BAD IDEA." The hedged market-maker earns Sharpe >4 on the same flow the naked seller earns 0.85–1.4 gross (Verdad Capital) — **the edge belongs to the hedger, not the seller.**

### 1.4 GEX / dealer gamma — mechanism real, retail signal unproven

The delta-hedging feedback mechanism is peer-reviewed and real (Baltussen et al., *JFE* 2021: intraday momentum on negative-gamma days, gross SR 1.73 — **no net Sharpe published**, costs conceded). But the best-identified study using *actual signed* dealer positions (Amaya et al., 442M Cboe SPX records) finds the effect **small** — median vol impact 0.08pp, max 3.3pp vs a 4.5pp normal daily vol swing — and the leading 0DTE paper (Dim–Eraker–Vilkov) finds 0DTE gamma **does not propagate volatility**. Vendor GEX adds two layers of sign-inference error, and an 8-year vendor backtest shows GEX's correlation with next-day vol collapses to ρ=−0.03 after controlling for VIX+IV. **Decision: GEX stays telemetry-only in this system — never a gate.** (Sources: SSRN 4692190; cboe.com gammasqueezes.pdf; JFE S0304405X21001598; flashalpha.com 8-yr backtest.)

### 1.5 Open-source reality check

No open-source 0DTE bot with credible public evidence of sustained live profitability exists (survey of GitHub, July 2026). The honest ones say so: IgorGanapolsky/trading (10k+ commits): "*This is not a profitable system today*" (PF 0.22). The value in public repos is **engineering patterns**, not strategy: atomic multi-leg staging (`transmit=False`), order-proposal state machines with broker reconciliation (9600dev/mmr), quote-driven limit ladders (thetagang), broker-side kill switches, and **graduation gates** before live capital (MEICAgent: ≥30 fills + positive expectancy + PF thresholds).

Your v1 bot already implements most of this (broker-truth invariant, confirm-flat, persisted risk state, kill rule) — the infrastructure is the part worth keeping.

### 1.6 Benchmark indices & sizing math — the sobering priors

The longest-running real evidence on systematic index premium selling (CBOE benchmark indices + peer-reviewed math), added 2026-07-19:

| Benchmark | Result | Implication |
|---|---|---|
| **PUT** (monthly ATM put-write), *live era 2007–2026* | **7.0%/yr, Sharpe 0.51** vs S&P **11.0%, 0.61** | Underperforms on raw AND risk-adjusted return once the backtest years are dropped (Cboe's own factsheet) |
| **WPUT** (weekly put-write) 2006–2018 | **4.51%, Sharpe 0.40** — worse than monthly PUT (5.97%, 0.50) despite collecting 37%/yr premium vs 22% | **Selling more often destroyed value.** A daily-frequency (0DTE) harvester starts with this prior against it |
| **CNDR** (iron condor index) | ~**1%/yr for 10–20 years** | The retail-favorite structure ≈ T-bills with tail risk |

- **Kelly math on your old trades:** a 9:1 risk/reward spread at a 90% win rate is *exactly* break-even — Kelly says bet **zero**; and full-Kelly size moves 10 points per 1-point error in estimated win rate. High-win-rate short premium cannot be sized rationally from small samples.
- **Frequency doesn't fix inference** (Broadie–Chernov–Johannes, *RFS* 2009; Merton 1980): the calendar span of data — not the number of trades — pins down the mean. Trading daily instead of monthly does not shorten the years needed to prove an edge. Deep-OTM put returns are statistically indistinguishable from "no mispricing at all."
- **A high Sharpe from selling options is manufacturable with zero skill** (Goetzmann et al., *RFS* 2007 — Sharpe is maximized by writing OTM options and hiding risk in the tail). Treat every impressive short-vol Sharpe, including §1.2's, accordingly.
- **Tail case studies (2018/2020/2024):** losses scale with *leverage and short-gamma convexity*, not the equity move — Feb-2018: S&P −4.2%, XIV −96% and terminated, while the *unlevered* PUT index lost only −4.8% that week. This is why the plan's tail-budget sizing + convexity hedge are non-negotiable, and why the strategy can only ever be run unlevered.

These priors *lower* the odds that §3's redesign passes its backtest gate. That is fine — the gate exists to find out, and a clean FAIL is a valid, money-saving result.

### 1.7 Current market state (verified live, week of 2026-07-13)

| Item | Value |
|---|---|
| SPY / SPX / VIX | $743.29 / 7,457.69 / **18.77** (VIX +12% Friday) |
| SPY realized vol 5/10/20/30-day | 10.3% / 10.8% / 12.0% / 15.7% |
| Implied − realized spread | **+3 to +8 vol pts → VRP positive** (favorable for disciplined short premium) |
| SPY 0DTE quoted spreads | ATM ~$0.01 (~0.4% of premium); 0.20Δ ~$0.01 (~1.6%); wings $0.01–0.04 but **8–67% of premium** |
| 4-leg condor round-trip spread cost | ~$0.06 vs credit → ~5% ($1.30 credit) to ~30% ($0.20 credit) |
| IBKR commissions, 4-leg × 4-lot round trip | **≈ $20.80** (32 contract-fills × $0.65) |
| Event risk | **FOMC July 28–29**; big-tech earnings this week |
| SPX 0DTE share | ~63% of SPX ADV (~3.3M contracts, June 2026 record) |

Two implications: (a) the regime is currently *favorable* to VRP harvesting (IV > RV), (b) your historical credits ($0.20–$1.30) put round-trip friction at **15–40%+ of max profit** — the evidence's cost mechanism, live in your own fills.

---

## 2. New flaws in the current bot (not in the v1 packet)

Microstructure and anti-gaming issues the architecture review didn't cover:

1. **Wrong instrument.** SPY options are American-style and physically settled — the entire assignment failure class (your −$365 and −$542 incidents) exists *only because of this choice*. SPX/XSP are European, cash-settled: a breached spread can expire and settle in cash, never becoming naked stock. XSP = 1/10 SPX, right-sized for a $100k sleeve, plus Section 1256 tax treatment (60/40) if this ever goes live. Trade-off: XSP quotes are wider than SPY's penny markets — must be measured in paper (gate G4).
   *Broker-policy evidence (verified from official docs, 2026-07-19):* on physically-settled expiries IBKR **starts discretionary force-liquidation of at-risk expiring positions ~2 hours before the close (~14:00 ET)** and may instead **let a long ITM leg lapse unexercised** — the documented "short leg assigned, long leg lapsed" trap that converts a defined-risk vertical into naked exposure (IBKR KB 1767; tastytrade and Robinhood publish the same right; every broker uses OCC's $0.01 auto-exercise, holder instructions to 17:25/17:30 ET). Two consequences: (a) never rely on the broker backstop — it is discretionary, can fire hours before your 15:30 flatten ladder, and can fire *against* you; (b) cash-settled XSP removes this entire policy surface, not just the assignment mechanics.
   *OCC mechanics (primary-source, 2026-07-19):* SPY options **trade until 16:15 ET but auto-exercise is decided on the 16:00 close** (OCC Rule 805, $0.01, uniform); holders can file contrary instructions until **17:30 ET** — so a post-close SPY move can get your short leg exercised against you while your long leg lapses, and **you don't learn the assignment until the next business day** (clearing members' own recorded objection: positions "unknown until Saturday," unwound "the following Monday"). No assignment-frequency statistic is published anywhere — any quoted % is unsourced. XSP expiring series simply stop at 16:00 and settle in cash the next morning at 1/10 the SPX close; the entire 16:00→17:30 uncertainty window does not exist.
2. **Wrong structure.** Tight verticals collecting $0.20–$1.30 against $5–$10 width are the net-negative row of §1.2. Risking ~$4–9 to make ~$1 with 15–40% friction is structurally unprofitable regardless of signal quality.
3. **Fixed 0.2% OTM strike selection ignores vol.** At VIX 12 that's ~0.5σ of a day; at VIX 30 it's noise. Strikes must be delta/expected-move-based, not %-based — otherwise position risk varies ~3× across regimes with identical sizing.
4. **Predictable, gameable behavior.** Entry rule fires at deterministic thresholds; forced flatten at exactly 15:55 daily. Forced, time-known flows are the easiest counterparty in the book. Randomize execution timing within windows; flatten via a ladder starting 15:30–15:45, not a single known timestamp.
5. **Entry timing never researched.** The bot enters whenever gates pass (often ~11:30). The academic net-positive results specifically use **10:00 ET entry** — after opening auction noise, before lunch liquidity trough. Liquidity and VRP capture vary sharply intraday; entry time is a first-class parameter, currently unmanaged.
6. **Premium stops are placebo at 0DTE** (documented 15-min stop lag, gamma ±100% in minutes). Your `stop_mult=999` accidentally acknowledges this — but the plan should be explicit: risk is controlled by *structure and size at entry*, spot-based defense, and time exits. Never by premium stops.
7. **SmartRouted BAG legging risk.** IBKR SmartRouted combos may leg each side separately for price improvement → one-leg-filled exposure. Use guaranteed/direct-routed combos, or atomic staging with `transmit=False` and cancel-on-partial (mmr pattern).
8. **The book is locally concave with no convexity anywhere.** Every position is short gamma; nothing in the account benefits from a tail. The two-engine principle (math&markets): pair short-premium with cheap long 1–7DTE wings so the *account* is globally convex, and size the short engine by tail budget: `qty ≤ ρ_tail × Account / L_max`.
9. **Commission drag unmodeled in the strategy design.** $20.80/round trip on trades whose max profit was often <$300 is a 7–20% haircut before the market moves. The structure must clear commissions **by design** (larger credit per unit of legs, fewer legs, or wider structures).
10. **Backtest ≠ live in five ways** (partially known): trade-price fills instead of NBBO, flat 2% slippage instead of ORATS-style width-based (75% single-leg / 53% multi-leg), $10k sizing vs $100k live, no liquidity gate, no event calendar. Nothing the current backtest says transfers to live.

---

## 3. The redesigned system — "VRP-Conditioned Defined-Risk Harvester"

Keep: the entire hardened execution layer (broker-truth invariant, confirm-flat, persisted risk state, reconcile, kill rule, dashboards). Replace: instrument, structure, signal, sizing, data, backtest.

### 3.1 The edge, stated precisely

Harvest the 0-day volatility risk premium **only when it is measurably present**, in the structures the evidence says survive costs, at the entry time the evidence tested, sized so the unsampled tail cannot kill the account, with a standing convexity hedge.

Why it should work (and its limits): 0DTE IV persistently exceeds subsequent realized vol by a small margin (VIX0DTE > VIX30 most of sample — Beckmeyer; OptionMetrics SPX 0DTE VRP mean ≈ 0.0028). The premium is real but thin, front-end-compressed, and decaying; it pays a Sharpe ≈ 0.8–0.9 net in ratio/basket structures. This is a *modest carry trade with catastrophic left tail*, run only under strict conditions — not an alpha machine.

### 3.2 Specification

**Instrument:** XSP (cash-settled, European). SPY only if XSP spreads measured in paper are >2× worse net-net.

**Entry window:** 09:55–10:05 ET (randomized within), one entry/day max. No entries: FOMC days ±1, CPI/NFP mornings, half-days, VIX > 30 (crash regime — premium is rich for a reason), or measured VRP signal below threshold.

**The VRP gate (the actual signal):**
```
vrp_signal = iv_0dte_atm − rv_trailing(20d, intraday-adjusted)
TRADE only if vrp_signal ≥ 2.0 vol pts             # premium demonstrably rich
              and term_slope = iv_0dte / iv_30d ≥ 1.0   # front-end elevated
              and no_event_today and vix < 30
```

**Structure (priority order, per evidence):**
1. **Put ratio spread** (e.g., buy 1× ~25Δ put, sell 2× ~12Δ puts, net credit ≥ 0): net SR 0.93 in sample. Defined behavior: profits in small down-moves, flat/small-win up, risk beyond the lower strike — which is why it carries the tail hedge and tail-budget sizing.
2. **Diversified basket** (small ratio + small strangle-width condor far OTM) once single-structure execution is proven.
3. Explicitly **banned**: tight iron condors / verticals with credit < 25% of width (the −0.20 net SR family — your current strategy).

**Convexity hedge (standing, non-negotiable):** long 1–7DTE ~5Δ SPX/XSP puts, budget 15–20% of expected monthly short-premium income. The account must be net long tail gamma. This converts "1–2 bad weeks give back everything" into a bounded drawdown.

**Sizing (tail budget, not margin):**
```
L_max  = worst-case structure loss at settlement (hedge included)
qty    = floor(0.5% × NetLiq / L_max)        # one day can never cost >0.5%
       capped by: margin, 1 concurrent, 4/day unchanged
```

**Exits:** profit target 25–40% of credit (0DTE decay is front-loaded; capture and leave). Time exit 14:30 if neither target nor defense hit. Spot-based defense unchanged (underlying at short strike → market out; on XSP a failed exit cash-settles rather than assigning). Flatten ladder 15:30→15:45, randomized. **No premium stops.**

**Execution:** guaranteed combo, limit at mid, ladder toward the market every 15–20s, abort entry if not filled in 90s (no chasing). Reuse v1's terminal-state/confirm-flat machinery unchanged.

### 3.3 Data plan (decision made for you)

| Purpose | Feed | Cost | Action |
|---|---|---|---|
| Live quotes | IBKR: US Securities Snapshot & Futures Value Bundle + US Equity & Options Add-On Streaming | **$10 + $4.50/mo** ($10 waived at ~$30 commissions/mo) | Subscribe in Client Portal → Market Data. **Requires the funded live account; then enable "share real-time data with paper" (takes ~24h; can't use live+paper simultaneously).** Set `market_data_type: 1`. |
| Backtest (NBBO) | **ThetaData Options Standard** | **$80/mo** (cancel after the study) | 8yr NBBO quotes + tick. One or two months of subscription is enough to build the dataset. |
| Keep | Polygon Starter | $29/mo (already paying) | Aggregates/IV telemetry. Confirmed (2026-07-05, empirically): **no NBBO on this plan** — it cannot do either job above. |

Total new spend: **~$14.50/mo ongoing + $80/mo for 1–2 months** of backtest data.

### 3.4 Realistic backtest protocol (pass/fail, not decoration)

1. ThetaData NBBO minute (or tick) quotes, XSP + SPX, 2022-05 → present (all-weekday 0DTE era only — earlier data is synthetic by construction).
2. Fills: cross the spread — ORATS model: **75% of quoted width single-leg, 53% multi-leg**; entry on the *next* quote after signal; widen fills 2× during first/last 15 min and event days.
3. Costs: $0.65/contract + $1 order minimums, per leg, open and close.
4. Sizing: the live $100k tail-budget rule, not $10k compounding.
5. Include: liquidity gate (skip if quoted width > 15% of credit), the VRP gate, event calendar, 10:00 entry.
6. Report: net expectancy/trade, PF, bootstrap 95% CI of $/trade, max DD, **and the March-2023-style worst weeks** shown separately.
7. **Pass criterion (pre-registered): net PF ≥ 1.15 with CI-lower > break-even on ≥ 350 trades.** Fail → do not proceed; the honest conclusion is "no edge at retail costs," and the project pivots or stops.

### 3.5 Daily ops (mostly already built)

Keep v1's runner (sync → test gate → auth → paper-guard → session → reconcile → dashboard). Add: pre-market VRP/event check that can declare NO-TRADE before entries; the weekly 2FA login is still the #1 operational killer — calendar it (Sunday evening), since it erased 3 of the last 5 sessions.

---

## 4. Implementation roadmap (hand-off ready)

**Phase 0 — decisions & data (week 1).** Subscribe IBKR bundles; enable paper sharing; flip `market_data_type: 1`; verify `marketDataType==1` ticks in logs. Subscribe ThetaData; pull XSP+SPX NBBO 2022-05→now; store parquet.

**Phase 1 — backtest harness (weeks 1–3).** New `research/nbbo_backtest.py` implementing §3.4. Validate the fill model by replaying the 6 real fills the bot has already made (predicted vs actual fill ≤ 1 tick error). Run the pre-registered study; publish PF/CI. **This phase decides everything.**

**Phase 2 — strategy modules (weeks 2–4, parallel).**
```
src/signals/vrp_gate.py        # iv_0dte_atm, rv_trailing, term_slope, no_event → TradeDecision
src/strategy/structures.py     # build_put_ratio(chain, target_deltas, min_credit) → Structure
src/strategy/tail_hedge.py     # maintain_long_wings(budget_pct, dte_range 1–7, delta≈5)
src/execution/ladder.py        # mid → market limit ladder, 15–20s steps, 90s abort
config: instrument=XSP, entry_window=09:55–10:05, bans tight verticals
```
Reuse untouched: `ibkr_broker` lifecycle, `reconcile`, `position_manager` (swap sizing fn), monitor/kill rule, dashboards.

**Phase 3 — paper graduation (weeks 4–12).** Run paper with live data. **Graduation gate (pre-registered): ≥100 fills, PF ≥ 1.15, bootstrap CI-lower > $0/trade, zero unmanaged-position incidents, hedge P&L behaving as designed.** The existing n≥500 kill rule stays as the outer bound.

**Phase 4 — go/no-go review.** Only after Phase 3 passes. Live sizing starts at 25% of paper size for a month.

---

## 5. Costs & honest expected value

| Item | Number |
|---|---|
| New data spend | ~$14.50/mo + $80/mo × 2 months |
| Evidence-based ceiling | Net SR ~0.8–0.9 (pre-decay, no crash in sample) on the short engine |
| On a $100k sleeve, tail-budgeted | Realistically **~$300–800/month** expectancy if the backtest passes — before the hedge costs 15–20% of it |
| Failure mode | One unsampled tail day without the hedge: −$5k to −$15k. With hedge + tail budget: bounded ≈ −0.5–1% |

If $300–800/mo on $100k (≈4–9%/yr) is not worth the operational effort to you, the rational alternative is documented in §1: the hedged market-makers earn the Sharpe; the passive index holder pays no friction at all. That is a legitimate conclusion of this research, not a failure.

---

## 6. Go/no-go — the exact conditions

**NO-GO today.** Flips to GO when ALL of:

- **G1** Real-time data verified live in logs (`marketDataType=1`).
- **G2** NBBO backtest passes pre-registered criterion (§3.4.7): PF ≥ 1.15, CI-lower > break-even, ≥350 trades, 2022-05→present.
- **G3** ≥100 paper fills meeting the graduation gate (§ Phase 3) with zero unmanaged-position incidents.
- **G4** XSP execution quality measured in paper: median effective spread ≤ 2× SPY-equivalent; else instrument decision revisited.
- **G5** Convexity hedge live and observed behaving (long wings marked daily).
- **G6** Ops: 4 consecutive weeks without a 2FA/Gateway-caused missed session.
- **G7** Half-day calendar implemented (known defect #3 from v1) — non-negotiable before any live dollar.

If G2 **fails**, the pre-registered conclusion is: *no retail-executable edge in 0DTE short premium at current costs* — stop or pivot (longer-DTE VRP, or index investing). Do not re-run with relaxed criteria; that is how every vendor backtest in §1.5 was produced.

---

## 7. Source index

Academic / regulatory: SSRN 4404704 (Beckmeyer et al. — retail 0DTE losses) · SSRN 4692190 (Dim–Eraker–Vilkov — gamma & vol propagation) · SSRN 4701401 (Almeida–Freire–Hizmeri — 0DTE asset pricing, edge decay) · SSRN 4590083 / RFS (Duarte et al. — look-ahead bias, Sharpe 0.5→5) · github.com/vilkovgr/0dte-strategies (net-of-cost Sharpes) · OptionMetrics 0DTE VRP blog.
Practitioner: Verdad Capital "Zero-Day Options" · Benn Eifert (QVR) threads · Theta Profits 9,000-trade log · OptionAlpha 0DTE studies (230k/25k trades; stop-lag) · EliteTrader SPXW threads · mathandmarkets two-engine 0DTE.
Infrastructure: IBKR market-data & commissions pages · ThetaData pricing · Databento OPRA plans · ORATS backtest methodology · Lumibot/optopsy/py_vollib · repos: thetagang, 9600dev/mmr, MEICAgent, IgorGanapolsky/trading, ib-api-reloaded/ib_async.
Market state: broker MCP live quotes (SPY/SPX/VIX, 2026-07-17 close) · Cboe volume releases · FOMC calendar.

*Prepared as the working master plan. Sections 3–4 are the blueprint to hand to a developer or another AI; Section 6 is the contract with yourself.*
