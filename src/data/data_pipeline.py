"""Data pipeline: download/cache bars, assemble the training set, and build live snapshots.

CLI:
    python -m src.data.data_pipeline --download --days 30
    python -m src.data.data_pipeline --build-training --days 30   # prints shape
"""
from __future__ import annotations

import argparse
from datetime import datetime
from typing import Optional

import pandas as pd

from ..common import MarketSnapshot, OptionRight, Regime
from ..execution.pricing import atm_strike, black_scholes
from ..signals.feature_engineering import build_features
from ..signals.labeling import make_labels
from ..signals.regime_classifier import classify_regime
from ..utils.logger import get_logger
from .free_feed import YFinanceFeed

log = get_logger("data_pipeline")


def get_feed(cfg):
    """Return a feed object matching cfg.data.provider (yfinance default; ibkr optional)."""
    provider = cfg.data.get("provider", "yfinance")
    if provider == "ibkr":
        from .ibkr_feed import IBKRFeed
        ib = cfg.execution.ibkr
        feed = IBKRFeed(host=ib.host, port=ib.paper_port, client_id=ib.client_id + 1,
                        symbol=cfg.symbol)
        feed.connect()
        return feed
    return YFinanceFeed(symbol=cfg.symbol, vix_symbol=cfg.data.get("vix_symbol", "^VIX"),
                        interval=cfg.data.get("interval", "1m"),
                        cache_dir=cfg.data.get("cache_dir", "data"))


def load_bars(cfg, days: int, download: bool = False) -> pd.DataFrame:
    feed = YFinanceFeed(symbol=cfg.symbol, vix_symbol=cfg.data.get("vix_symbol", "^VIX"),
                        interval=cfg.data.get("interval", "1m"),
                        cache_dir=cfg.data.get("cache_dir", "data"))
    if not download:
        cached = feed.load_cached(days)
        if cached is not None and not cached.empty:
            return cached
    return feed.download(days)


def build_training_set(cfg, bars: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Return (X, y) ready for LightGBM, dropping rows without a full forward label window."""
    mp = cfg.model_params
    features = build_features(bars)
    labels, valid = make_labels(
        bars["close"],
        horizon_bars=mp["label"]["horizon_bars"],
        threshold_pct=mp["label"]["threshold_pct"],
    )
    mask = valid & features.notna().all(axis=1)
    X = features[mask]
    y = labels[mask]
    log.info("Training set: %d rows, %.1f%% positive", len(X), 100 * y.mean() if len(y) else 0)
    return X, y


def build_snapshot(cfg, bars: pd.DataFrame, model=None, sentiment=None,
                   minutes_to_close: Optional[float] = None) -> MarketSnapshot:
    """Build a MarketSnapshot from the most recent bars for the live/paper loop."""
    features = build_features(bars)
    frow = features.iloc[-1]
    price = float(bars["close"].iloc[-1])

    # ATM 0DTE greeks via Black-Scholes using the realized-vol IV proxy.
    iv = float(frow["rv_annual_proxy"]) or 0.20
    iv = min(max(iv, 0.05), 3.0)
    if minutes_to_close is None:
        minutes_to_close = _minutes_to_close(bars.index[-1], cfg)
    strike = atm_strike(price)
    greeks = black_scholes(price, strike, minutes_to_close, iv, OptionRight.CALL)

    regime = classify_regime(frow)
    ml_prob = 0.5
    if model is not None:
        ml_prob = model.predict_one(frow.to_dict())

    sentiment_score = 0.0
    if sentiment is not None:
        sentiment_score = 0.0  # headlines wired in by the caller when available

    high5 = float(bars["high"].tail(5).max())
    low5 = float(bars["low"].tail(5).min())
    vwap = price * (1 + float(frow["vwap_dev"]))

    return MarketSnapshot(
        timestamp=bars.index[-1].to_pydatetime(),
        spy_price=price,
        spy_volume=float(bars["volume"].iloc[-1]),
        vwap=vwap,
        atr_5min=float(frow["atr_5"]),
        rvol=float(frow["rvol"]),
        high_5min=high5,
        low_5min=low5,
        iv=iv,
        vix=float(frow["vix"]),
        delta=greeks.delta,
        gamma=greeks.gamma,
        theta=greeks.theta,
        sentiment_score=sentiment_score,
        regime=regime,
        ml_prob_up=ml_prob,
        features=frow.to_dict(),
    )


def _minutes_to_close(ts, cfg) -> float:
    """Minutes from bar timestamp to market close (America/New_York)."""
    local = ts.tz_convert("America/New_York") if ts.tz else ts
    close_h, close_m = map(int, cfg.session.market_close.split(":"))
    minutes = (close_h * 60 + close_m) - (local.hour * 60 + local.minute)
    return float(max(minutes, 1))


def _main() -> None:
    from ..utils.config import load_config

    parser = argparse.ArgumentParser(description="Data pipeline")
    parser.add_argument("--download", action="store_true", help="force fresh download")
    parser.add_argument("--build-training", action="store_true")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    cfg = load_config()
    bars = load_bars(cfg, args.days, download=args.download)
    print(f"Loaded {len(bars)} bars from {bars.index[0]} to {bars.index[-1]}")

    if args.build_training:
        X, y = build_training_set(cfg, bars)
        print(f"X shape: {X.shape}, positive rate: {y.mean():.3f}")


if __name__ == "__main__":
    _main()
