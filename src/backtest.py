"""Event-driven backtester.

Walks historical SPY minute bars. At each bar it:
  1. Re-prices and exits open positions (SL / TP / time-stop) via SimBroker.
  2. Builds a MarketSnapshot, asks the SignalGenerator for a decision.
  3. Sizes + opens a trade if risk gates and session windows allow.

Option P&L is MODELED from the underlying (Black-Scholes) with pessimistic slippage,
commission, and spread. Treat the resulting stats as an optimistic ceiling on the strategy's
skill, not a promise of live returns. See ARCHITECTURE.md "Known limitations".

    python -m src.backtest --days 30
"""
from __future__ import annotations

import argparse
from datetime import datetime, time

import numpy as np
import pandas as pd

from .common import MarketSnapshot, Signal
from .data.data_pipeline import load_bars
from .execution.pricing import atm_strike, black_scholes
from .execution.position_manager import PositionManager
from .execution.sim_broker import SimBroker
from .learning.evaluator import PerformanceMonitor, summarize
from .signals.feature_engineering import build_features
from .signals.lightgbm_model import DirectionalClassifier
from .signals.regime_classifier import classify_regime
from .signals.signal_generator import SignalGenerator
from .common import OptionRight
from .utils.config import load_config
from .utils.logger import get_logger

log = get_logger("backtest")


def _parse_hhmm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def run_backtest(cfg, days: int = 30, verbose: bool = True) -> dict:
    bars = load_bars(cfg, days, download=False)
    if bars.empty:
        raise RuntimeError("No cached data. Run: python -m src.data.data_pipeline --download")

    features = build_features(bars)
    local_idx = bars.index.tz_convert("America/New_York")

    # ML probabilities (vectorized) if a model exists; else neutral 0.5.
    if DirectionalClassifier.exists(cfg.model.path, cfg.model.meta_path):
        model = DirectionalClassifier.load(cfg.model.path, cfg.model.meta_path)
        probs = model.predict_proba(features)
        log.info("Loaded model for backtest.")
    else:
        probs = np.full(len(features), 0.5)
        log.warning("No model found; running rules-only (train first for ML signals).")

    broker = SimBroker(cfg, starting_equity=cfg.risk["account"]["starting_equity"])
    siggen = SignalGenerator(cfg)
    pm = PositionManager(cfg)
    monitor = PerformanceMonitor()

    close_t = _parse_hhmm(cfg.session.market_close)
    no_new_t = _parse_hhmm(cfg.session.no_new_trades_after)
    flatten_t = _parse_hhmm(cfg.session.flatten_time)
    open_t = _parse_hhmm(cfg.session.market_open)

    closes = bars["close"].to_numpy()
    highs = bars["high"].to_numpy()
    lows = bars["low"].to_numpy()
    vols = bars["volume"].to_numpy()

    for i in range(len(bars)):
        now = local_idx[i].to_pydatetime()
        t = now.time()
        price = float(closes[i])

        # 1. Manage open positions first.
        for result in broker.poll_exits(price, now):
            monitor.update(result)
            pm.record_result(result.pnl)

        # End-of-day flatten.
        if t >= flatten_t:
            for result in broker.flatten(now):
                monitor.update(result)
                pm.record_result(result.pnl)
            continue

        if t < open_t or t >= no_new_t:
            continue

        frow = features.iloc[i]
        minutes_to_close = max((close_t.hour * 60 + close_t.minute)
                               - (t.hour * 60 + t.minute), 1)
        iv = min(max(float(frow["rv_annual_proxy"]) or 0.20, 0.05), 3.0)
        strike = atm_strike(price)
        g = black_scholes(price, strike, minutes_to_close, iv, OptionRight.CALL)

        snap = MarketSnapshot(
            timestamp=now, spy_price=price, spy_volume=float(vols[i]),
            vwap=price * (1 + float(frow["vwap_dev"])),
            atr_5min=float(frow["atr_5"]), rvol=float(frow["rvol"]),
            high_5min=float(highs[max(i - 4, 0):i + 1].max()),
            low_5min=float(lows[max(i - 4, 0):i + 1].min()),
            iv=iv, vix=float(frow["vix"]), delta=g.delta, gamma=g.gamma, theta=g.theta,
            regime=classify_regime(frow), ml_prob_up=float(probs[i]),
            features=frow.to_dict(),
        )

        decision = siggen.generate(snap)
        if decision.signal == Signal.NO_TRADE:
            continue

        ok, _ = pm.can_open(now, broker.account_value(), len(broker.open_positions()))
        if not ok:
            continue
        intent = pm.build_intent(decision.signal, snap, broker.account_value(), minutes_to_close)
        if intent is None:
            continue
        broker.place_bracket(intent)
        pm.on_open()

    # Flatten anything still open at the very end.
    for result in broker.flatten(local_idx[-1].to_pydatetime()):
        monitor.update(result)

    report = summarize(monitor.trades)
    result = {
        "report": report.as_dict(),
        "final_equity": broker.account_value(),
        "starting_equity": cfg.risk["account"]["starting_equity"],
        "n_bars": len(bars),
        "period": f"{bars.index[0]} -> {bars.index[-1]}",
    }
    if verbose:
        print("\n=== Backtest ===")
        print(result["period"])
        print(report.pretty())
        print(f"equity: {result['starting_equity']:.2f} -> {result['final_equity']:.2f}")
        print("(Modeled option fills — optimistic ceiling, not live-accurate.)")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the 0DTE strategy")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    cfg = load_config()
    run_backtest(cfg, days=args.days)


if __name__ == "__main__":
    main()
