"""Main paper/live loop.

    python -m src.main --broker sim  --mode paper     # offline simulation (default)
    python -m src.main --broker ibkr --mode paper     # IBKR paper account (needs TWS/Gateway)
    python -m src.main --broker ibkr --mode live      # real money — requires live_confirmed

Loop per tick: pull latest bars -> snapshot -> anomaly check -> signal -> memory gate ->
risk gate -> broker -> record -> monitor. Trades only inside RTH; flattens before the close.
"""
from __future__ import annotations

import argparse
import time as _time
from datetime import datetime, time

from .common import Signal
from .data.data_pipeline import build_snapshot
from .data.free_feed import YFinanceFeed
from .execution.position_manager import PositionManager
from .execution.sim_broker import SimBroker
from .learning.anomaly_detector import AnomalyAction, AnomalyDetector
from .learning.evaluator import PerformanceMonitor
from .signals.lightgbm_model import DirectionalClassifier
from .signals.sentiment_analyzer import SentimentAnalyzer
from .signals.signal_generator import SignalGenerator
from .utils.alerts import Alerter
from .utils.config import load_config
from .utils.logger import get_logger, setup_logging

log = get_logger("main")


def make_broker(cfg, broker_name: str, mode: str):
    if broker_name == "ibkr":
        from .execution.ibkr_broker import IBKRBroker
        broker = IBKRBroker(cfg, mode=mode)
    else:
        broker = SimBroker(cfg)
    broker.connect()
    return broker


def make_feed(cfg):
    if cfg.data.get("provider") == "ibkr":
        from .data.ibkr_feed import IBKRFeed
        ib = cfg.execution.ibkr
        feed = IBKRFeed(host=ib.host, port=ib.paper_port, client_id=ib.client_id + 1,
                        symbol=cfg.symbol)
        feed.connect()
        return feed
    return YFinanceFeed(symbol=cfg.symbol, vix_symbol=cfg.data.get("vix_symbol", "^VIX"),
                        interval=cfg.data.get("interval", "1m"),
                        cache_dir=cfg.data.get("cache_dir", "data"))


def _parse(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def run(cfg, broker_name: str, mode: str, once: bool = False) -> None:
    setup_logging(cfg.logging.get("level", "INFO"), cfg.logging.get("dir", "logs"))
    alerter = Alerter.from_config(cfg)

    feed = make_feed(cfg)
    broker = make_broker(cfg, broker_name, mode)
    siggen = SignalGenerator(cfg)
    pm = PositionManager(cfg)
    monitor = PerformanceMonitor()
    anomaly = AnomalyDetector(cfg)
    sentiment = SentimentAnalyzer.from_config(cfg)

    model = None
    if DirectionalClassifier.exists(cfg.model.path, cfg.model.meta_path):
        model = DirectionalClassifier.load(cfg.model.path, cfg.model.meta_path)
        log.info("Loaded directional model.")
    else:
        log.warning("No model found; rules-only. Train with: python -m src.learning.trainer --train")

    open_t, close_t = _parse(cfg.session.market_open), _parse(cfg.session.market_close)
    no_new_t = _parse(cfg.session.no_new_trades_after)
    flatten_t = _parse(cfg.session.flatten_time)
    poll = cfg.execution.get("poll_seconds", 30)

    alerter.send(f"Bot starting: broker={broker_name} mode={mode}")
    log.info("Loop start: broker=%s mode=%s poll=%ss", broker_name, mode, poll)

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

            # Manage open positions every tick.
            for result in broker.poll_exits(price, now):
                monitor.update(result)
                pm.record_result(result.pnl)
                alerter.send(f"Closed {result.right.value} {result.strike} "
                             f"{result.exit_reason.value} pnl=${result.pnl:.2f}")

            # Session flatten.
            if t >= flatten_t:
                closed = broker.flatten(now)
                for r in closed:
                    monitor.update(r)
                if closed:
                    alerter.send(f"End-of-day flatten: {len(closed)} positions")

            in_session = open_t <= t < no_new_t
            if in_session:
                snap = build_snapshot(cfg, bars, model=model, sentiment=sentiment)

                # Anomaly gate.
                anomaly.observe(snap.features.get("ret_1", 0.0), snap.iv)
                a = anomaly.check(snap.features.get("ret_1", 0.0), snap.iv)
                if a.action == AnomalyAction.HALT:
                    closed = broker.flatten(now)
                    for r in closed:
                        monitor.update(r)
                    alerter.send(f"ANOMALY {a.kinds}: halted + flattened", level="WARN")
                elif a.action == AnomalyAction.NONE:
                    decision = siggen.generate(snap)
                    if decision.signal != Signal.NO_TRADE:
                        ok, why = pm.can_open(now, broker.account_value(),
                                              len(broker.open_positions()))
                        if ok:
                            mtc = max((close_t.hour * 60 + close_t.minute)
                                      - (t.hour * 60 + t.minute), 1)
                            intent = pm.build_intent(decision.signal, snap,
                                                     broker.account_value(), mtc)
                            if intent is not None:
                                broker.place_bracket(intent)
                                pm.on_open()
                                alerter.send(f"OPEN {intent.right.value} {intent.strike} "
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
        broker.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="0DTE SPY bot main loop")
    parser.add_argument("--broker", choices=["sim", "ibkr"], default=None)
    parser.add_argument("--mode", choices=["paper", "live"], default=None)
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    args = parser.parse_args()

    cfg = load_config()
    broker_name = args.broker or cfg.execution.get("broker", "sim")
    mode = args.mode or cfg.execution.get("mode", "paper")

    if mode == "live" and broker_name == "ibkr" and not cfg.execution.get("live_confirmed", False):
        raise SystemExit("Refusing live mode: set execution.live_confirmed: true in config first.")

    run(cfg, broker_name, mode, once=args.once)


if __name__ == "__main__":
    main()
