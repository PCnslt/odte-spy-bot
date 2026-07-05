"""Live paper-trading loop — real-time IBKR data, real routing to an IBKR paper account.

    python -m src.main --mode paper    # IBKR paper account (needs TWS/Gateway in paper mode)
    python -m src.main --mode live     # real money — requires execution.live_confirmed: true

Per tick: pull real-time SPY bars from IBKR -> snapshot -> anomaly check -> signal -> resolve
the REAL 0DTE contract (real premium + ATR from IBKR) -> risk gate -> IBKR bracket order.
Trades only inside RTH; flattens before the close. No simulated data or prices anywhere.
"""
from __future__ import annotations

import argparse
import time as _time
from datetime import datetime, time

from .common import Signal
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
    strike_offset = cfg.execution.option.get("strike_offset", 0)

    alerter.send(f"Bot starting: IBKR {mode} on port {port}")
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
            for r in broker.poll_exits(price, now):
                monitor.update(r)
                pm.record_result(r.pnl)
                alerter.send(f"Closed {r.right.value}{r.strike:g} {r.exit_reason.value} "
                             f"pnl=${r.pnl:.2f}")

            if t >= flatten_t:
                for r in broker.flatten(now):
                    monitor.update(r)
                alerter.send("End-of-day flatten.")

            if open_t <= t < no_new_t:
                snap = build_snapshot(cfg, bars, model=model)
                vol = float(snap.features.get("rv_annual", 0.0))
                ret1 = float(snap.features.get("ret_1", 0.0))
                anomaly.observe(ret1, vol)
                a = anomaly.check(ret1, vol)
                if a.action == AnomalyAction.HALT:
                    for r in broker.flatten(now):
                        monitor.update(r)
                    alerter.send(f"ANOMALY {a.kinds}: halted + flattened", level="WARN")
                elif a.action == AnomalyAction.NONE:
                    decision = siggen.generate(snap)
                    if decision.signal != Signal.NO_TRADE:
                        ok, why = pm.can_open(now, broker.account_value(),
                                              len(broker.open_positions()))
                        if ok:
                            right = "C" if decision.signal == Signal.BUY_CALL else "P"
                            opt = feed.resolve_option(right, price, now.date(), strike_offset)
                            if opt:
                                entry = round(opt["entry_price"] *
                                              (1 + cfg.risk["commissions"]["slippage_frac"]), 2)
                                intent = pm.build_intent(
                                    decision.signal, snap, broker.account_value(),
                                    opt["label"], opt["strike"], entry, opt["atr"])
                                if intent is not None:
                                    broker.place_bracket(intent)
                                    pm.on_open()
                                    alerter.send(f"OPEN {right}{opt['strike']:g} "
                                                 f"x{intent.quantity} @ {intent.entry_price} "
                                                 f"(p={decision.ml_prob:.2f})")
            if once:
                break
            _time.sleep(poll)
    except KeyboardInterrupt:
        log.info("Interrupted; flattening.")
        broker.flatten(datetime.now())
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
    args = parser.parse_args()

    cfg = load_config()
    mode = args.mode or cfg.execution.get("mode", "paper")
    if mode == "live" and not cfg.execution.get("live_confirmed", False):
        raise SystemExit("Refusing live mode: set execution.live_confirmed: true in config first.")
    run(cfg, mode, once=args.once)


if __name__ == "__main__":
    main()
