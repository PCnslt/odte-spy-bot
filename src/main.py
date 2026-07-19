"""Live paper-trading loop — real-time IBKR data, real routing to an IBKR paper account.

    python -m src.main --mode paper    # IBKR paper account (needs TWS/Gateway in paper mode)
    python -m src.main --mode live     # real money — requires execution.live_confirmed: true

STRATEGY: defined-risk 0DTE CREDIT SPREADS (premium selling) — the only variant that survived
walk-forward research (long premium was decisively negative; see README). Bullish signal ->
sell a bull put spread; bearish -> sell a bear call spread. Atomic combo orders, sized by max
loss, managed to profit-target / stop / max-hold / EOD flatten.

Per tick: real SPY bars -> snapshot -> anomaly check -> signal -> resolve REAL spread legs ->
risk gate -> IBKR combo order -> manage open spread on real leg prices. No simulated data.
"""
from __future__ import annotations

import argparse
import time as _time
from datetime import datetime, time, timedelta

from .common import Signal
from .research.spreads import SpreadTrade
from .data.data_pipeline import build_snapshot
from .data.ibkr_feed import IBKRFeed
from .execution.ibkr_broker import IBKRBroker
from .execution.position_manager import RISK_STATE_PATH, PositionManager
from .execution.risk import (assign_arm, defense_triggered, gap_exceeds, liquidity_ok,
                             spread_ev, stop_cost)
from .learning.anomaly_detector import AnomalyAction, AnomalyDetector
from .learning.evaluator import PerformanceMonitor
from .signals.cost_meta_labeler import CostMetaLabeler
from .signals.lightgbm_model import DirectionalClassifier
from .signals.range_model import RangeForecaster, atr_range_estimate, dynamic_short_otm
from .signals.signal_generator import SignalGenerator
from .utils.alerts import Alerter
from .utils.config import load_config
from .utils.events import EventGuard
from .utils.logger import get_logger, setup_logging
from .utils.trade_log import TradeLog

log = get_logger("main")


def _parse(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def _loop_stalled(last_beat: float, now: float, threshold_s: float) -> bool:
    """True if the trading loop hasn't ticked within threshold_s. Incident 2026-07-07: a hung
    IBKR call froze the whole loop, and because it never EXITED, launchd's crash-restart could
    not recover it. The watchdog below force-exits on a stall so the runner relaunches."""
    return (now - last_beat) > threshold_s


def healthcheck(cfg, mode: str = "paper") -> bool:
    """Minimal AUTHENTICATED check for schedulers: the port being open is not enough —
    Gateway can be running but logged out (e.g. after weekly 2FA expiry). Connect, demand
    a managed account and a positive net liquidation, disconnect. Exit 0/1."""
    ib = cfg.execution.ibkr
    port = ib.paper_port if mode == "paper" else ib.live_port
    broker = IBKRBroker(cfg, mode=mode)
    try:
        broker.connect()
        accounts = broker.ib.managedAccounts()
        netliq = broker.account_value()
        ok = bool(accounts) and netliq > 0
        print(f"HEALTHCHECK {'PASS' if ok else 'FAIL'}: accounts={accounts} "
              f"netliq=${netliq:,.0f} port={port}")
        return ok
    except Exception as exc:
        print(f"HEALTHCHECK FAIL: {exc}")
        return False
    finally:
        try:
            broker.disconnect()
        except Exception:
            pass


def selftest(cfg, mode: str = "paper") -> bool:
    """Validate the live path end-to-end WITHOUT placing an order: connect IBKR data + broker,
    pull real SPY bars, resolve a real 0DTE contract, read account value. Prints a checklist.

        python -m src.main --selftest        # needs TWS/Gateway (paper) running, API enabled
    """
    setup_logging(cfg.logging.get("level", "INFO"), cfg.logging.get("dir", "logs"))
    ib = cfg.execution.ibkr
    port = ib.paper_port if mode == "paper" else ib.live_port
    checks: list[tuple[str, bool, str]] = []

    def check(name, fn):
        try:
            detail = fn()
            checks.append((name, True, detail))
        except Exception as exc:
            checks.append((name, False, str(exc)[:160]))
            raise

    feed = IBKRFeed(host=ib.host, port=port, client_id=ib.client_id + 5, symbol=cfg.symbol,
                    exchange=ib.exchange, currency=ib.currency,
                    market_data_type=int(ib.get("market_data_type", 3)))
    broker = IBKRBroker(cfg, mode=mode)
    from datetime import datetime as _dt, timedelta as _td

    def _next_expiry():
        """Today during the week; next Monday on weekends (no SPY options expire Sat/Sun)."""
        d = _dt.now().date()
        while d.weekday() >= 5:
            d += _td(days=1)
        return d
    ok = True
    try:
        check("feed connects", lambda: (feed.connect(), f"{ib.host}:{port}")[1])
        bars = feed.latest_bars(lookback_minutes=30)
        check("SPY real-time bars", lambda: f"{len(bars)} bars, last={bars['close'].iloc[-1]:.2f}"
              if not bars.empty else (_ for _ in ()).throw(RuntimeError("no bars")))
        price = float(bars["close"].iloc[-1])
        opt = feed.resolve_option("C", price, _next_expiry(), 0)
        check("resolve 0DTE contract", lambda: (f"{opt['label']} premium={opt['entry_price']:.2f} "
              f"atr={opt['atr']:.2f}") if opt else (_ for _ in ()).throw(RuntimeError("none")))
        spread = feed.resolve_spread("bull_put", price, _next_expiry(),
                                     cfg.spread.width, cfg.spread.short_otm_pct)
        check("resolve credit spread", lambda: (
            f"{spread['kind']} short={spread['short'].strike:g} long={spread['long'].strike:g} "
            f"credit~{spread['credit']:.2f}") if spread
            else (_ for _ in ()).throw(RuntimeError("no spread legs")))
        check("broker connects", lambda: (broker.connect(),
              f"NetLiq=${broker.account_value():,.0f}")[1])
    except Exception:
        ok = False
    finally:
        try:
            feed.disconnect()
        except Exception:
            pass
        try:
            broker.disconnect()
        except Exception:
            pass

    print("\n=== IBKR live-path self-test (%s) ===" % mode)
    for name, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name:24s} {detail}")
    if not any(c[1] for c in checks):
        print("  (Is TWS/IB Gateway running in %s mode with the API enabled on port %d?)"
              % (mode, port))
    print("RESULT:", "PASS — live path is wired." if ok else "FAIL — see above.")
    return ok


def _assert_eastern_host() -> None:
    """Audit m2: all session logic uses naive datetime.now() and ASSUMES the host clock is
    America/New_York. Refuse to trade on a mis-zoned host (fail closed) rather than trade
    the wrong hours. Override with ODTE_TZ_OVERRIDE=1 only if you know what you're doing."""
    import os
    from zoneinfo import ZoneInfo

    if os.getenv("ODTE_TZ_OVERRIDE") == "1":
        return
    now = datetime.now()
    et = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    if abs((now - et).total_seconds()) > 120:
        raise SystemExit(
            f"Host clock is not America/New_York (local {now:%H:%M} vs ET {et:%H:%M}). "
            "Session logic would trade the wrong hours. Set the OS timezone to ET or "
            "export ODTE_TZ_OVERRIDE=1 to bypass.")


def run(cfg, mode: str, once: bool = False, daily: bool = False) -> None:
    """`daily=True` exits cleanly after the session close (for schedulers like launchd)."""
    _assert_eastern_host()
    setup_logging(cfg.logging.get("level", "INFO"), cfg.logging.get("dir", "logs"))
    alerter = Alerter.from_config(cfg)

    ib = cfg.execution.ibkr
    port = ib.paper_port if mode == "paper" else ib.live_port
    feed = IBKRFeed(host=ib.host, port=port, client_id=ib.client_id + 1, symbol=cfg.symbol,
                    exchange=ib.exchange, currency=ib.currency,
                    market_data_type=int(ib.get("market_data_type", 3)))
    feed.connect()

    broker = IBKRBroker(cfg, mode=mode)
    broker.connect()

    # Crash recovery (self-audit R6): positions in the account that this process didn't
    # open are unmanaged 0DTE risk — flatten them immediately, fail closed.
    n_orphans = broker.flatten_orphans()
    if n_orphans:
        alerter.send(f"RECOVERY: flattened {n_orphans} orphaned option position(s) from a "
                     f"prior crash.", level="WARN")

    siggen = SignalGenerator(cfg)
    # Durable daily guardrails: the halt / trade-cap / loss-brake must survive a watchdog
    # force-exit + launchd relaunch, otherwise a crash re-arms a fresh loss budget.
    pm = PositionManager(cfg, state_path=RISK_STATE_PATH)
    monitor = PerformanceMonitor()
    anomaly = AnomalyDetector(cfg)

    model = None
    if DirectionalClassifier.exists(cfg.model.path, cfg.model.meta_path):
        model = DirectionalClassifier.load(cfg.model.path, cfg.model.meta_path)
        log.info("Loaded directional model (features: %d).", len(model.feature_columns))
    else:
        log.warning("No model; rules-only. Train: python -m src.learning.trainer --train")

    # Range forecaster: the spread-seller's target. Fail-safe: ATR estimate when absent.
    range_model = None
    if RangeForecaster.exists(cfg.model.range_path, cfg.model.range_meta_path):
        range_model = RangeForecaster.load(cfg.model.range_path, cfg.model.range_meta_path)
        log.info("Loaded range forecaster.")
    else:
        log.warning("No range model; using ATR-based range estimate. Train to enable.")

    # Breach models for the EV (premium-richness) gate. Fail-safe: absent -> gate inactive.
    breach = {}
    for side in ("dn", "up"):
        bp = cfg.model.get(f"breach_{side}_path")
        bm = cfg.model.get(f"breach_{side}_meta_path")
        if bp and bm and DirectionalClassifier.exists(bp, bm):
            breach[side] = DirectionalClassifier.load(bp, bm)
    if breach:
        log.info("Loaded breach models: %s", sorted(breach))

    # H10 shadow cost-quality meta-labeler. Fail-closed: absent -> predict_one returns 0.5.
    # Observational ONLY — it logs P(BAD_FILL), it never gates a trade.
    cost_labeler = None
    _cmp, _cmm = cfg.model.get("cost_meta_path"), cfg.model.get("cost_meta_meta_path")
    if _cmp and _cmm and CostMetaLabeler.exists(_cmp, _cmm):
        cost_labeler = CostMetaLabeler.load(_cmp, _cmm)
        log.info("Loaded cost-meta-labeler (shadow).")

    intel = cfg.intelligence
    events = EventGuard(intel.get("events_file", "config/events.yaml"))
    tradelog = TradeLog(cfg.memory.get("trade_log_path", "trades.db"))

    # Polygon client for entry-time IV capture (telemetry only; fail-safe to None).
    iv_client = None
    try:
        from .data.polygon_options import PolygonOptions
        iv_client = PolygonOptions.from_config(cfg)
    except Exception as exc:
        log.info("IV capture disabled (no Polygon client): %s", exc)

    open_t, close_t = _parse(cfg.session.market_open), _parse(cfg.session.market_close)
    no_new_t, flatten_t = _parse(cfg.session.no_new_trades_after), _parse(cfg.session.flatten_time)
    poll = cfg.execution.get("poll_seconds", 30)

    sp = cfg.spread
    max_hold = timedelta(minutes=sp.get("max_hold_minutes", 240))
    pt_frac, stop_mult = sp.profit_target_frac, sp.stop_mult
    commission = cfg.risk["commissions"]["per_contract"]
    risk_pct = cfg.risk["per_trade"]["risk_pct"]
    max_ct = cfg.risk["per_trade"]["max_contracts"]
    # Size and halt against a fixed risk BUDGET, not the full paper NetLiq. A $1M paper balance
    # makes risk_pct·equity sizing inert (always clamps to max_ct) and the daily-loss halt
    # (max_daily_loss_pct·equity) unreachable. Falls back to NetLiq if no budget is configured.
    risk_budget = float(cfg.risk["per_trade"].get("risk_budget_usd") or 0) or broker.account_value()

    open_spreads: list[dict] = []
    gap_day = None          # opening-gap guard state (checked once per session)
    gap_block = False
    gex = None              # R10/H7: session GEX telemetry (fetched once with gap check)

    def _record_close(pos: dict, exit_cost: float, reason: str, now,
                      exit_cost_est: float | None = None, limit_exit: bool = False,
                      exit_cost_fill: float | None = None) -> None:
        tr = SpreadTrade(pos["open_time"], now, pos["spread"]["kind"],
                         round(pos["credit"], 2), round(max(exit_cost, 0.0), 2),
                         pos["quantity"], reason, commission=commission * pos["quantity"] * 4)
        monitor.update(tr)
        pm.record_result(tr.pnl)
        if pos.get("trade_id") is not None:
            try:
                tradelog.close_trade(
                    pos["trade_id"], closed_at=now.isoformat(), exit_reason=reason,
                    exit_cost_est=exit_cost_est if exit_cost_est is not None else exit_cost,
                    # Record the ACTUAL fill whenever one was captured — limit OR market. It was
                    # gated on limit_exit before, so every market exit (the common case) lost its
                    # real fill and exit-slippage analytics could never populate.
                    exit_cost_fill=exit_cost_fill,
                    credit_fill=pos["credit"], pnl=tr.pnl, limit_exit=limit_exit)
            except Exception as exc:
                log.warning("TradeLog close failed: %s", exc)
        alerter.send(f"CLOSE {pos['spread']['kind']} x{pos['quantity']} {reason} "
                     f"credit={tr.credit:.2f} cost={tr.exit_cost:.2f} pnl=${tr.pnl:.2f}")

    def _manage_spreads(now, spot: float | None = None, force: bool = False) -> None:
        for pos in list(open_spreads):
            # --- pending close (limit OR market): capture the ACTUAL fill (self-audit R6:
            # H3 needs real market-exit fills, not estimates) ---
            if pos.get("close_trade") is not None:
                done, actual_cost = broker.close_fill(pos)
                if done:
                    _record_close(pos, actual_cost if actual_cost is not None
                                  else pos["close_cost"], pos["close_reason"], now,
                                  exit_cost_est=pos["close_cost"],
                                  limit_exit=not pos.get("close_market", False),
                                  exit_cost_fill=actual_cost)
                    open_spreads.remove(pos)
                elif pos.get("close_market", False):
                    waited = (now - pos["close_since"]).total_seconds()
                    if now.time() >= close_t:
                        # Session over and the close still hasn't confirmed. Escalate to PER-LEG
                        # market exits — a deep-ITM leg makes the BAG illiquid even though each
                        # leg still trades — then demand BROKER PROOF of flat before letting go.
                        log.error("Close unconfirmed at session end; escalating to per-leg exits.")
                        try:
                            broker.close_legs_individually(pos)
                        except Exception as exc:
                            log.error("Per-leg close failed: %s", exc)
                        flat = False
                        try:
                            flat = broker.confirm_flat(broker.spread_con_ids(pos), timeout=10.0)
                        except Exception as exc:
                            log.error("confirm_flat failed: %s", exc)
                        if not flat:
                            log.critical("NOT FLAT at session end — 0DTE assignment risk.")
                            alerter.send(
                                f"CRITICAL: {pos['spread']['kind']} x{pos['quantity']} is NOT "
                                f"FLAT at session end — 0DTE assignment risk tonight. "
                                f"Run `python -m src.main --flatten`.", level="CRITICAL")
                        # NEVER fabricate a P&L for a close we could not confirm. On 2026-07-09
                        # the exit cost fell back to the entry credit, booking a real ~-$400 loss
                        # as -$5.20. Record it as UNKNOWN (pnl NULL); the NetLiq ledger carries
                        # the money truth. Feed the risk halt the WORST case so it errs toward
                        # stopping, never toward trading on a loss it can't see.
                        worst = -abs((pos["spread"]["width"] - pos["credit"])
                                     * 100 * pos["quantity"])
                        pm.record_result(worst)
                        if pos.get("trade_id") is not None:
                            try:
                                tradelog.mark_unconfirmed(pos["trade_id"],
                                                          closed_at=now.isoformat())
                            except Exception as exc:
                                log.warning("mark_unconfirmed failed: %s", exc)
                        open_spreads.remove(pos)
                    elif waited > 90:
                        # Unconfirmed but the session is STILL OPEN: re-send the close and keep
                        # tracking. Do NOT book a fake fill and drop a live position — that
                        # orphaned a breached spread into overnight assignment on 2026-07-09
                        # (recorded -$5 while the account fell -$365).
                        log.warning("Market close unconfirmed after %.0fs; re-sending.", waited)
                        pos["close_trade"] = broker.escalate_close(pos)
                        pos["close_since"] = now
                elif force or ((now - pos["close_since"]).total_seconds()
                               > intel.get("limit_exit_escalate_s", 120)):
                    # Escalate limit -> market, but keep tracking to capture the real fill.
                    pos["close_trade"] = broker.escalate_close(pos)
                    pos["close_market"] = True
                    pos["close_since"] = now
                continue

            filled, credit = broker.spread_fill_status(pos)
            if not filled:
                # Entry not (fully) filled. On force or after 3 min, cancel the working entry.
                # close_credit_spread cancels it and, if some contracts DID fill, markets out
                # exactly that quantity and returns the unwind trade — track it so its real P&L
                # is recorded (not dropped/zeroed by reconcile) and it isn't left unmanaged.
                if force or now - pos["open_time"] > timedelta(minutes=3):
                    tr = broker.close_credit_spread(pos)
                    if tr is not None:
                        pos["quantity"] = int(pos.get("_unwound_qty") or pos["quantity"])
                        pos.update(close_trade=tr, close_since=now,
                                   close_reason="entry_partial_unwind",
                                   close_cost=credit, close_market=True)
                        log.info("Partial entry unwound x%d; tracking the real exit fill.",
                                 pos["quantity"])
                    else:
                        open_spreads.remove(pos)
                        log.info("Unfilled spread entry cancelled.")
                continue
            pos["credit"] = credit
            cost = feed.spread_close_cost(pos["spread"])

            # EOD flatten (force) must NEVER be blocked on a missing quote — market out
            # regardless. Bug fixed 2026-07-09: the cost-None guard used to sit ABOVE the
            # flatten branch, so a late-day OTM leg with no quote was left open into 0DTE
            # expiry → overnight assignment / naked-share risk (the invariant is: nothing held
            # overnight). The real fill is captured on the next poll and recorded.
            if force:
                tr = broker.close_credit_spread(pos)   # market SELL of the filled qty
                if tr is not None:
                    pos.update(close_trade=tr, close_since=now, close_reason="flatten",
                               close_cost=(cost if cost is not None else credit),
                               close_market=True)
                else:
                    open_spreads.remove(pos)
                continue

            # STRIKE DEFENSE — evaluated on the UNDERLYING, and deliberately ABOVE the
            # option-quote guard. It is the ONLY exit that does not depend on option quotes,
            # which this Gateway serves 15 MINUTES DELAYED (marketDataType=3, verified
            # 2026-07-10). If SPY reaches the short strike, get out now — quote or no quote.
            # It used to sit below `cost is None`, so a missing/stale quote skipped the brake
            # at precisely the moment it was needed: that path is how 2026-07-09's breach rode
            # to expiry and assigned into naked short stock.
            if intel.get("defense_enabled", False) and spot is None:
                # Data outage: no underlying price, so the quote-free stop cannot evaluate and
                # the position is unmanaged until the 15:55 flatten. Make that visible.
                log.warning("STRIKE DEFENSE unavailable: no underlying price (data outage) with "
                            "an open %s — position unmanaged until flatten.", pos["spread"]["kind"])
            if (intel.get("defense_enabled", False) and spot is not None
                    and defense_triggered(pos["spread"]["kind"], spot,
                                          float(pos["spread"]["short"].strike),
                                          intel.get("defense_buffer_pct", 0.001))):
                log.warning("STRIKE DEFENSE: spot %.2f threatens short %.0f (%s) — market exit.",
                            spot, float(pos["spread"]["short"].strike), pos["spread"]["kind"])
                alerter.send(f"STRIKE DEFENSE: exiting {pos['spread']['kind']} "
                             f"(spot {spot:.2f} vs short {float(pos['spread']['short'].strike):.0f})",
                             level="WARN")
                tr = broker.close_credit_spread(pos)   # urgent -> market
                if tr is not None:
                    pos.update(close_trade=tr, close_since=now, close_reason="strike_defense",
                               close_cost=(cost if cost is not None else credit),
                               close_market=True)
                else:
                    open_spreads.remove(pos)
                continue

            if cost is None:
                continue
            tp_cost = credit * (1 - pt_frac)
            sl_cost = stop_cost(credit, pos["spread"]["width"], stop_mult,
                                sp.get("stop_width_frac"))
            reason = None
            if cost <= tp_cost:
                reason, cost = "take_profit", tp_cost
            elif cost >= sl_cost:                       # strike_defense handled above, on spot
                reason, cost = "stop_loss", sl_cost
            elif now - pos["open_time"] >= max_hold:
                reason = "time_stop"
            if reason:
                # Non-urgent exits (profit target / time stop) try a LIMIT at the current
                # mid first — don't donate the spread crossing when there's no rush.
                urgent = reason in ("stop_loss", "strike_defense")   # force handled above
                if (not urgent and intel.get("limit_exits", True)):
                    tr = broker.close_credit_spread(pos, limit_cost=cost)
                    if tr is not None:
                        pos.update(close_trade=tr, close_since=now,
                                   close_reason=reason, close_cost=cost)
                        continue  # fill/escalation handled on subsequent polls
                # Urgent path: market order, but STILL track it to record the actual fill
                # (self-audit R6: without this, H3 has no real market-exit data).
                tr = broker.close_credit_spread(pos)
                if tr is not None:
                    pos.update(close_trade=tr, close_since=now, close_reason=reason,
                               close_cost=cost, close_market=True)
                else:  # entry was never filled; nothing to unwind
                    open_spreads.remove(pos)

    entries_enabled = bool(cfg.execution.get("entries_enabled", True))
    if not entries_enabled:
        log.warning("ENTRIES DISABLED (execution.entries_enabled=false): the bot will sweep, "
                    "manage, flatten, reconcile and publish — but open NO new positions.")
        alerter.send("Bot starting in OBSERVE-ONLY mode: no new entries.", level="WARN")

    alerter.send(f"Bot starting: IBKR {mode} on port {port} — CREDIT SPREADS "
                 f"(width ${sp.width}, PT {pt_frac:.0%}, stop {stop_mult}x)")

    # Watchdog: if a broker call hangs and the loop stops ticking, force-exit so launchd
    # relaunches us. A hung (non-exiting) process defeats the crash-restart net. Heartbeat is
    # bumped at the top of every iteration; a stall well beyond one poll means we're wedged.
    import os as _os
    import signal as _signal
    import threading as _threading
    _hb = [_time.time()]
    _shutdown = [False]
    _stall_s = max(150.0, 4.0 * poll)

    def _watchdog() -> None:
        while True:
            _time.sleep(30)
            if _shutdown[0]:
                return          # never force-exit while the emergency flatten is running
            if _loop_stalled(_hb[0], _time.time(), _stall_s):
                log.error("WATCHDOG: trading loop stalled %.0fs (>%ds) — forcing exit for "
                          "relaunch.", _time.time() - _hb[0], int(_stall_s))
                _os._exit(1)
    _threading.Thread(target=_watchdog, daemon=True, name="watchdog").start()

    def _emergency_drain() -> None:
        """Flatten everything and prove it, bumping the heartbeat so the watchdog can't kill us
        mid-flatten. Used by SIGTERM/SIGINT and by ANY unhandled loop error."""
        _shutdown[0] = True
        try:
            for _ in range(5):
                _hb[0] = _time.time()
                _manage_spreads(datetime.now(), force=True)
                if not open_spreads:
                    break
                _time.sleep(2)
            if open_spreads:
                log.warning("%d close(s) unconfirmed at shutdown; sweeping orphans.",
                            len(open_spreads))
                broker.flatten_orphans()
        except Exception as exc:
            log.error("Emergency flatten failed: %s — run `python -m src.main --flatten`.", exc)

    # launchd sends SIGTERM on logout/restart/bootout. Python's default disposition kills the
    # process immediately — `finally` never runs and live 0DTE positions are stranded into
    # expiry. Route SIGTERM into the same drain path as Ctrl-C.
    def _on_sigterm(_sig, _frame):
        if _shutdown[0]:
            # Already draining — a second SIGTERM must NOT interrupt the flatten in progress.
            log.warning("SIGTERM during shutdown drain — ignoring.")
            return
        log.warning("SIGTERM received — flattening before exit.")
        raise KeyboardInterrupt
    try:
        _signal.signal(_signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):  # not the main thread
        pass

    try:
        while True:
            now = datetime.now()
            _hb[0] = _time.time()
            t = now.time()
            bars = None
            try:
                bars = feed.latest_bars(lookback_minutes=120)
            except Exception as exc:
                log.error("Data fetch failed: %s", exc)
            if bars is None or bars.empty:
                # A market-data outage must NEVER strand an open 0DTE position. Previously the
                # loop `continue`d here, skipping the whole management state machine — including
                # the 15:55 flatten — while the watchdog stayed green. Manage positions anyway:
                # the force-flatten path markets out without needing a quote.
                if open_spreads:
                    log.warning("No market data; still managing %d open position(s).",
                                len(open_spreads))
                _manage_spreads(now, spot=None, force=(t >= flatten_t))
                if daily and t >= close_t and not open_spreads:
                    log.info("Session over (%s, no data); daily mode exiting.", t)
                    break
                if once:
                    break
                _time.sleep(poll)
                continue

            price = float(bars["close"].iloc[-1])

            # 1. Manage open spreads on real leg prices (incl. strike defense); EOD flatten.
            _manage_spreads(now, spot=price, force=(t >= flatten_t))

            # 1b. CONTINUOUS BROKER-TRUTH RECONCILIATION. If the book thinks we are flat but the
            # account still holds SPY legs or shares, that is unmanaged risk — a prior breach's
            # assignment, a crashed order, an untracked fill. Sweep it, and REFUSE new entries
            # until the broker confirms flat. The startup sweep only *places* orders; a
            # pre-market stock order often won't fill, and we must never stack a fresh spread on
            # top of an unhedged assigned position.
            unmanaged = []
            if not open_spreads:
                try:
                    unmanaged = broker.orphan_positions()
                except Exception as exc:
                    log.error("Broker-truth check failed: %s", exc)
                if unmanaged:
                    log.critical("UNMANAGED: %d broker position(s) with an empty book — "
                                 "sweeping; entries BLOCKED until flat.", len(unmanaged))
                    alerter.send(f"CRITICAL: {len(unmanaged)} unmanaged SPY position(s) at the "
                                 f"broker. Sweeping; new entries blocked until flat.",
                                 level="CRITICAL")
                    try:
                        broker.flatten_orphans()
                    except Exception as exc:
                        log.error("Unmanaged-position sweep failed: %s", exc)

            # Scheduler mode: once the session is over and everything is flat, exit for the day.
            if daily and t >= close_t and not open_spreads:
                # Belt-and-suspenders: never end the day leaving a real position the book lost
                # track of. Expired 0DTE legs can't be traded (they settle); the STK-aware sweep
                # here + at next startup clears any shares a breach assigned.
                orphans = broker.orphan_positions()
                if orphans:
                    log.warning("Daily exit: %d orphan leg(s) still at broker; sweeping.",
                                len(orphans))
                    broker.flatten_orphans()
                log.info("Session over (%s); daily mode exiting.", t)
                break

            # 2. New entries inside the session window only — never while the account holds
            # something we aren't managing (1b). Entries are gated by the internal risk
            # manager + config only; the dashboard is view-only (owner order, 2026-07-20).
            if open_t <= t < no_new_t and not unmanaged and entries_enabled:
                # Opening-gap guard (once per session): a big overnight gap means a violent
                # open the warming-up anomaly detector can't see yet — sit the day out.
                if gap_day != now.date():
                    gap_day = now.date()
                    g = feed.overnight_gap()
                    gap_block = gap_exceeds(g, intel.get("gap_guard_pct", 0.01))
                    if gap_block:
                        alerter.send(f"GAP GUARD: overnight gap {g:+.2%} >= "
                                     f"{intel.get('gap_guard_pct', 0.01):.2%} — no new "
                                     f"entries today.", level="WARN")
                    # R10/H7: session GEX telemetry (naive dealer-gamma from the real
                    # 0DTE chain). Observational only until H7's pre-registered test.
                    gex = None
                    if iv_client is not None:
                        try:
                            gex = iv_client.gex_snapshot(now.date())
                            if gex:
                                log.info("GEX: net=%.3g gamma_wall=%s (n=%d)",
                                         gex["gex_net"], gex["gamma_wall"], gex["n_used"])
                        except Exception as exc:
                            log.info("GEX snapshot failed: %s", exc)
                if gap_block:
                    if once:
                        break
                    _time.sleep(poll)
                    continue
                snap = build_snapshot(cfg, bars, model=model)
                vol = float(snap.features.get("rv_annual", 0.0))
                ret1 = float(snap.features.get("ret_1", 0.0))
                anomaly.observe(ret1, vol)
                a = anomaly.check(ret1, vol)
                if a.action == AnomalyAction.HALT:
                    _manage_spreads(now, force=True)
                    alerter.send(f"ANOMALY {a.kinds}: halted + flattened", level="WARN")
                elif a.action == AnomalyAction.NONE:
                    decision = siggen.generate(snap)
                    if decision.signal != Signal.NO_TRADE:
                        ok, why = pm.can_open(now, risk_budget, len(open_spreads))
                        # --- event guard (config/events.yaml) ---
                        safety = intel.get("range_safety_mult", 1.25)
                        ev = events.check(now.date()) if ok else None
                        if ev and ev["action"] == "block":
                            log.info("Event day (%s): new entries blocked.", ev["name"])
                            ok = False
                        elif ev and ev["action"] == "widen":
                            safety *= 2
                            log.info("Event day (%s): doubled range safety.", ev["name"])
                        if ok:
                            kind = ("bull_put" if decision.signal == Signal.BUY_CALL
                                    else "bear_call")
                            # --- expected-range forecast (telemetry always; strike
                            # placement only when use_range_strikes is enabled) ---
                            horizon = intel.get("range_horizon_bars", 60)
                            if range_model is not None:
                                range_pred = range_model.predict_one(snap.features)
                            else:
                                range_pred = atr_range_estimate(snap.atr_5min, price, horizon)
                            if intel.get("use_range_strikes", False):
                                otm = dynamic_short_otm(sp.short_otm_pct, range_pred, safety,
                                                        intel.get("max_short_otm_pct", 0.01))
                            else:
                                otm = sp.short_otm_pct
                            if otm is None:
                                log.info("Skipped %s: forecast range %.4f needs strike "
                                         "beyond max OTM cap.", kind, range_pred)
                            else:
                                # Width A/B (pre-registered H2b): deterministic per-entry arm.
                                if intel.get("width_experiment_enabled", False):
                                    width = float(assign_arm(
                                        now.strftime("%Y-%m-%dT%H:%M"),
                                        list(intel.get("width_arms", [sp.width]))))
                                else:
                                    width = sp.width
                                spread = feed.resolve_spread(kind, price, now.date(),
                                                             width, otm)
                                if spread:
                                    # --- real leg quotes: liquidity gate + mid pricing ---
                                    q = feed.leg_quotes(spread)
                                    if q is not None:
                                        spread["credit"] = q["mid_credit"]
                                        if not liquidity_ok(q["short_bid"], q["short_ask"],
                                                            q["long_bid"], q["long_ask"],
                                                            q["mid_credit"],
                                                            intel.get("liquidity_max_frac", 0.25)):
                                            log.info("Skipped %s: spread too illiquid for "
                                                     "credit %.2f", kind, q["mid_credit"])
                                            spread = None
                                    elif intel.get("require_quotes", False):
                                        log.info("Skipped %s: leg quotes unavailable.", kind)
                                        spread = None
                                # --- EV (premium-richness) gate ---
                                ev = None
                                if spread and intel.get("use_ev_gate", False):
                                    side = "dn" if kind == "bull_put" else "up"
                                    if side in breach:
                                        p_b = breach[side].predict_one(snap.features)
                                        ev = spread_ev(spread["credit"], p_b,
                                                       pt_frac, stop_mult)
                                        if ev < intel.get("min_ev", 0.0):
                                            log.info("Skipped %s: EV %.2f < min (credit %.2f,"
                                                     " P(breach)=%.2f)", kind, ev,
                                                     spread["credit"], p_b)
                                            spread = None
                                # A crossed/garbage quote can make credit >= width. That is not a
                                # free trade — it's bad data, and `// max_loss` would raise
                                # ZeroDivisionError, crash the loop, and (via relaunch) reset the
                                # daily risk counters. Refuse the trade instead.
                                if spread and (spread["width"] - spread["credit"]) * 100 <= 0:
                                    log.warning("Skipped %s: credit %.2f >= width %.2f — bad "
                                                "quote, non-positive max loss.", kind,
                                                spread["credit"], spread["width"])
                                    spread = None
                                if spread and spread["credit"] >= sp.min_credit:
                                    max_loss = (spread["width"] - spread["credit"]) * 100
                                    qty = max(1, min(max_ct, int(
                                        (risk_pct * risk_budget) // max_loss)))
                                    limit_frac = (intel.get("entry_limit_frac", 0.95)
                                                  if q is not None else 0.9)
                                    pos = broker.place_credit_spread(
                                        spread, qty, min_credit=spread["credit"] * limit_frac)
                                    if pos:
                                        open_spreads.append(pos)
                                        pm.on_open()
                                        # Audit m1: log the counterfactual — what would the
                                        # OTHER width arm's credit have been right now?
                                        alt_credit = None
                                        if intel.get("width_experiment_enabled", False):
                                            arms = [float(a) for a in
                                                    intel.get("width_arms", [])]
                                            others = [a for a in arms if a != width]
                                            if others:
                                                alt = feed.resolve_spread(
                                                    kind, price, now.date(), others[0], otm)
                                                if alt:
                                                    alt_credit = alt["credit"]
                                        # --- TradeLog: full decision context (the training
                                        # data every deferred idea is waiting for) ---
                                        try:
                                            p_dn = (breach["dn"].predict_one(snap.features)
                                                    if "dn" in breach else None)
                                            p_up = (breach["up"].predict_one(snap.features)
                                                    if "up" in breach else None)
                                            # Short-leg market-implied risk (R12): one
                                            # snapshot call -> IV + delta -> P(touch).
                                            iv_short = short_delta = ptouch = None
                                            if iv_client is not None:
                                                from .data.polygon_options import (
                                                    PolygonOptions as _P, prob_touch as _pt)
                                                tick = _P.option_ticker(
                                                    float(spread["short"].strike),
                                                    "P" if kind == "bull_put" else "C",
                                                    now.date())
                                                snap_c = iv_client.contract_snapshot(tick)
                                                iv_short = snap_c.get("iv")
                                                short_delta = snap_c.get("delta")
                                                ptouch = _pt(short_delta)
                                            # Trailing 60-min realized vol (H1's RV term).
                                            # R8 #2: TODAY's session only — near the open,
                                            # tail(60) would mix in yesterday's bars and
                                            # the overnight gap, contaminating RV.
                                            _et = bars.index.tz_convert("America/New_York")
                                            _today = bars[_et.date == now.date()]
                                            r60 = _today["close"].pct_change().tail(60)
                                            rv_60m = (float(r60.std()) * (252 * 390) ** 0.5
                                                      if len(r60.dropna()) >= 30 else None)
                                            # H10 SHADOW cost-quality: real leg half-spreads +
                                            # cost context -> P(BAD_FILL). Logged only; the
                                            # labeler NEVER gates a trade (it's 0.5 until a
                                            # model exists). No entry logic reads this.
                                            short_hs = ((q["short_ask"] - q["short_bid"]) / 2
                                                        if q else None)
                                            long_hs = ((q["long_ask"] - q["long_bid"]) / 2
                                                       if q else None)
                                            mins_close = max(0.0, (close_t.hour * 60
                                                             + close_t.minute)
                                                             - (t.hour * 60 + t.minute))
                                            _wall = (gex or {}).get("gamma_wall")
                                            _hsf = ((short_hs + long_hs) / spread["credit"]
                                                    if (short_hs is not None
                                                        and long_hs is not None
                                                        and spread["credit"]) else None)
                                            cost_feats = {
                                                "short_half_spread": short_hs,
                                                "long_half_spread": long_hs,
                                                "half_spread_frac": _hsf,
                                                "minutes_into_session": snap.features.get(
                                                    "minutes_into_session"),
                                                "minutes_to_close": mins_close,
                                                "rv_annual": snap.features.get("rv_annual"),
                                                "credit": spread["credit"],
                                                "width": spread["width"],
                                                "gex_net": (gex or {}).get("gex_net"),
                                                "gamma_wall_dist": (abs(price - _wall) / price
                                                                    if _wall else None)}
                                            p_bad_fill = (cost_labeler.predict_one(cost_feats)
                                                          if cost_labeler is not None else 0.5)
                                            pos["trade_id"] = tradelog.open_trade(
                                                opened_at=now.isoformat(), kind=kind,
                                                short_strike=float(spread["short"].strike),
                                                long_strike=float(spread["long"].strike),
                                                width=spread["width"], quantity=qty,
                                                credit_est=spread["credit"],
                                                alt_width_credit_est=alt_credit,
                                                spot=price,
                                                regime=snap.regime.value,
                                                ml_prob=decision.ml_prob,
                                                range_pred=range_pred,
                                                p_breach_dn=p_dn, p_breach_up=p_up,
                                                iv_short=iv_short,
                                                rv_annual=snap.features.get("rv_annual"),
                                                rv_60m=rv_60m,
                                                rvol=snap.rvol, atr_5=snap.atr_5min,
                                                minutes_into_session=snap.features.get(
                                                    "minutes_into_session"),
                                                gex_net=(gex or {}).get("gex_net"),
                                                gamma_wall=(gex or {}).get("gamma_wall"),
                                                short_delta=short_delta, prob_touch=ptouch,
                                                iv_atm=(gex or {}).get("atm_iv"),
                                                skew_25d=(gex or {}).get("skew_25d"),
                                                short_half_spread=short_hs,
                                                long_half_spread=long_hs,
                                                minutes_to_close=mins_close,
                                                p_bad_fill=p_bad_fill)
                                        except Exception as exc:
                                            log.warning("TradeLog open failed: %s", exc)
                                        alerter.send(
                                            f"OPEN {kind} x{qty} "
                                            f"short={spread['short'].strike:g} "
                                            f"long={spread['long'].strike:g} "
                                            f"credit~{spread['credit']:.2f} otm={otm:.3%} "
                                            f"range={range_pred:.3%} (p={decision.ml_prob:.2f})")
                                elif spread:
                                    log.info("Skipped %s: credit %.2f < min %.2f", kind,
                                             spread["credit"], sp.min_credit)
            if once:
                break
            _time.sleep(poll)
    except (KeyboardInterrupt, SystemExit):
        log.info("Interrupted; flattening.")
        _emergency_drain()
    except Exception as exc:
        # Any unexpected error used to fall straight through `finally` (which only disconnects),
        # leaving live 0DTE positions to expire into assignment. Drain first, then re-raise.
        log.critical("Unhandled error in trading loop: %s — emergency flatten.", exc,
                     exc_info=True)
        alerter.send(f"CRITICAL: bot crashed ({exc}); emergency flatten attempted.",
                     level="CRITICAL")
        _emergency_drain()
        raise
    finally:
        rep = monitor.report()
        alerter.send(f"Session end: {rep.pretty()}")
        log.info("Final: %s", rep.pretty())
        feed.disconnect()
        broker.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="0DTE SPY bot — live IBKR loop")
    parser.add_argument("--mode", choices=["paper", "live"], default=None)
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    parser.add_argument("--daily", action="store_true",
                        help="exit after the session close (for launchd/cron scheduling)")
    parser.add_argument("--selftest", action="store_true",
                        help="validate the IBKR live path (no orders placed) and exit")
    parser.add_argument("--healthcheck", action="store_true",
                        help="authenticated Gateway check for schedulers; exit 0/1")
    parser.add_argument("--flatten", action="store_true",
                        help="recovery: close ALL option positions in the account and exit")
    args = parser.parse_args()

    cfg = load_config()
    mode = args.mode or cfg.execution.get("mode", "paper")
    if mode == "live" and not cfg.execution.get("live_confirmed", False):
        raise SystemExit("Refusing live mode: set execution.live_confirmed: true in config first.")
    if args.healthcheck:
        raise SystemExit(0 if healthcheck(cfg, mode) else 1)
    if args.flatten:
        broker = IBKRBroker(cfg, mode=mode)
        # Distinct client id: reusing the live session's id (17) would kick the running bot off
        # its connection -> watchdog exit -> relaunch.
        broker.client_id = 48
        broker.connect()
        # Sweep, then PROVE flat via ib.positions() — not the count of orders sent. A pre-market
        # stock MarketOrder is held, not filled, so "N orders sent" is not "flat". Retry, and
        # exit NON-ZERO if still not flat so the caller (the runner's red-test path) can't claim
        # the account is safe when it isn't.
        flat = False
        for attempt in range(6):
            broker.flatten_orphans()
            con_ids = [getattr(p.contract, "conId", None) for p in broker.orphan_positions()]
            if not con_ids:
                flat = True
                break
            flat = broker.confirm_flat(con_ids, timeout=10.0)
            if flat:
                break
            log.warning("--flatten: not flat yet (attempt %d); retrying.", attempt + 1)
        remaining = len(broker.orphan_positions())
        broker.disconnect()
        if flat and remaining == 0:
            print("Account confirmed FLAT.")
            raise SystemExit(0)
        print(f"NOT FLAT — {remaining} position(s) remain. Manual intervention required.")
        raise SystemExit(2)
    if args.selftest:
        raise SystemExit(0 if selftest(cfg, mode) else 1)
    run(cfg, mode, once=args.once, daily=args.daily)


if __name__ == "__main__":
    main()
