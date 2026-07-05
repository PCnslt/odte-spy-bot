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
from .execution.position_manager import PositionManager
from .learning.anomaly_detector import AnomalyAction, AnomalyDetector
from .learning.evaluator import PerformanceMonitor
from .signals.lightgbm_model import DirectionalClassifier
from .signals.signal_generator import SignalGenerator
from .utils.alerts import Alerter
from .utils.config import load_config
from .utils.logger import get_logger, setup_logging

log = get_logger("main")


def _parse(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


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
                    exchange=ib.exchange, currency=ib.currency)
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


def run(cfg, mode: str, once: bool = False) -> None:
    setup_logging(cfg.logging.get("level", "INFO"), cfg.logging.get("dir", "logs"))
    alerter = Alerter.from_config(cfg)

    ib = cfg.execution.ibkr
    port = ib.paper_port if mode == "paper" else ib.live_port
    feed = IBKRFeed(host=ib.host, port=port, client_id=ib.client_id + 1, symbol=cfg.symbol,
                    exchange=ib.exchange, currency=ib.currency)
    feed.connect()

    broker = IBKRBroker(cfg, mode=mode)
    broker.connect()

    siggen = SignalGenerator(cfg)
    pm = PositionManager(cfg)
    monitor = PerformanceMonitor()
    anomaly = AnomalyDetector(cfg)

    model = None
    if DirectionalClassifier.exists(cfg.model.path, cfg.model.meta_path):
        model = DirectionalClassifier.load(cfg.model.path, cfg.model.meta_path)
        log.info("Loaded directional model (features: %d).", len(model.feature_columns))
    else:
        log.warning("No model; rules-only. Train: python -m src.learning.trainer --train")

    open_t, close_t = _parse(cfg.session.market_open), _parse(cfg.session.market_close)
    no_new_t, flatten_t = _parse(cfg.session.no_new_trades_after), _parse(cfg.session.flatten_time)
    poll = cfg.execution.get("poll_seconds", 30)

    sp = cfg.spread
    max_hold = timedelta(minutes=sp.get("max_hold_minutes", 240))
    pt_frac, stop_mult = sp.profit_target_frac, sp.stop_mult
    commission = cfg.risk["commissions"]["per_contract"]
    risk_pct = cfg.risk["per_trade"]["risk_pct"]
    max_ct = cfg.risk["per_trade"]["max_contracts"]

    open_spreads: list[dict] = []

    def _record_close(pos: dict, exit_cost: float, reason: str, now) -> None:
        tr = SpreadTrade(pos["open_time"], now, pos["spread"]["kind"],
                         round(pos["credit"], 2), round(max(exit_cost, 0.0), 2),
                         pos["quantity"], reason, commission=commission * pos["quantity"] * 4)
        monitor.update(tr)
        pm.record_result(tr.pnl)
        alerter.send(f"CLOSE {pos['spread']['kind']} x{pos['quantity']} {reason} "
                     f"credit={tr.credit:.2f} cost={tr.exit_cost:.2f} pnl=${tr.pnl:.2f}")

    def _manage_spreads(now, force: bool = False) -> None:
        for pos in list(open_spreads):
            filled, credit = broker.spread_fill_status(pos)
            if not filled:
                # Entry never filled: cancel after 3 minutes (or on force) and forget.
                if force or now - pos["open_time"] > timedelta(minutes=3):
                    broker.close_credit_spread(pos)
                    open_spreads.remove(pos)
                    log.info("Unfilled spread entry cancelled.")
                continue
            pos["credit"] = credit
            cost = feed.spread_close_cost(pos["spread"])
            if cost is None:
                continue
            tp_cost = credit * (1 - pt_frac)
            sl_cost = min(credit * stop_mult, pos["spread"]["width"])
            reason = None
            if force:
                reason = "flatten"
            elif cost <= tp_cost:
                reason, cost = "take_profit", tp_cost
            elif cost >= sl_cost:
                reason, cost = "stop_loss", sl_cost
            elif now - pos["open_time"] >= max_hold:
                reason = "time_stop"
            if reason:
                broker.close_credit_spread(pos)
                _record_close(pos, cost, reason, now)
                open_spreads.remove(pos)

    alerter.send(f"Bot starting: IBKR {mode} on port {port} — CREDIT SPREADS "
                 f"(width ${sp.width}, PT {pt_frac:.0%}, stop {stop_mult}x)")
    try:
        while True:
            now = datetime.now()
            t = now.time()
            try:
                bars = feed.latest_bars(lookback_minutes=120)
            except Exception as exc:
                log.error("Data fetch failed: %s", exc)
                if once:
                    break
                _time.sleep(poll)
                continue
            if bars.empty:
                log.warning("No bars returned.")
                if once:
                    break
                _time.sleep(poll)
                continue

            price = float(bars["close"].iloc[-1])

            # 1. Manage open spreads on real leg prices; EOD flatten.
            _manage_spreads(now, force=(t >= flatten_t))

            # 2. New entries inside the session window only.
            if open_t <= t < no_new_t:
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
                        ok, why = pm.can_open(now, broker.account_value(), len(open_spreads))
                        if ok:
                            kind = ("bull_put" if decision.signal == Signal.BUY_CALL
                                    else "bear_call")
                            spread = feed.resolve_spread(kind, price, now.date(),
                                                         sp.width, sp.short_otm_pct)
                            if spread and spread["credit"] >= sp.min_credit:
                                max_loss = (spread["width"] - spread["credit"]) * 100
                                qty = max(1, min(max_ct, int(
                                    (risk_pct * broker.account_value()) // max_loss)))
                                pos = broker.place_credit_spread(
                                    spread, qty, min_credit=spread["credit"] * 0.9)
                                if pos:
                                    open_spreads.append(pos)
                                    pm.on_open()
                                    alerter.send(
                                        f"OPEN {kind} x{qty} short={spread['short'].strike:g} "
                                        f"long={spread['long'].strike:g} "
                                        f"credit~{spread['credit']:.2f} "
                                        f"(p={decision.ml_prob:.2f})")
                            elif spread:
                                log.info("Skipped %s: credit %.2f < min %.2f", kind,
                                         spread["credit"], sp.min_credit)
            if once:
                break
            _time.sleep(poll)
    except KeyboardInterrupt:
        log.info("Interrupted; flattening.")
        _manage_spreads(datetime.now(), force=True)
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
    parser.add_argument("--selftest", action="store_true",
                        help="validate the IBKR live path (no orders placed) and exit")
    args = parser.parse_args()

    cfg = load_config()
    mode = args.mode or cfg.execution.get("mode", "paper")
    if mode == "live" and not cfg.execution.get("live_confirmed", False):
        raise SystemExit("Refusing live mode: set execution.live_confirmed: true in config first.")
    if args.selftest:
        raise SystemExit(0 if selftest(cfg, mode) else 1)
    run(cfg, mode, once=args.once)


if __name__ == "__main__":
    main()
