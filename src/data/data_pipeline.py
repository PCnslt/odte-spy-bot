"""Data pipeline: pull REAL SPY history (+ real VIX if entitled) from Polygon, assemble the
training set, and build MarketSnapshots. No modeled data anywhere.

CLI:
    python -m src.data.data_pipeline --download --days 30
    python -m src.data.data_pipeline --build-training --days 30
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

import pandas as pd

from ..common import MarketSnapshot
from ..signals.feature_engineering import build_features
from ..signals.labeling import make_labels
from ..signals.regime_classifier import classify_regime
from ..utils.logger import get_logger
from .polygon_options import PolygonError, PolygonOptions, _cache_fresh, _day_is_complete

log = get_logger("data_pipeline")


def get_polygon(cfg) -> PolygonOptions:
    return PolygonOptions.from_config(cfg)


def _date_range(days: int) -> tuple[date, date]:
    end = datetime.now().date()
    start = end - timedelta(days=days)
    return start, end


def load_bars(cfg, days: int, download: bool = False,
              poly: PolygonOptions | None = None) -> pd.DataFrame:
    """Real SPY minute bars over the last `days`, with a real `vix` column when entitled."""
    poly = poly or get_polygon(cfg)
    start, end = _date_range(days)

    cache = poly.cache_dir / f"spy_1m_{start:%Y%m%d}_{end:%Y%m%d}.parquet"
    # Same-day partial-session guard (audit C2): only serve/write caches whose final day
    # was complete at write time.
    if not download and cache.exists() and _cache_fresh(cache, end):
        return pd.read_parquet(cache)

    spy = poly.stock_history(start, end, symbol=cfg.symbol)
    if spy.empty:
        raise RuntimeError("Polygon returned no SPY bars — check plan/date range/API key.")

    include_vix = cfg.data.get("include_vix", True)
    vix_symbol = cfg.data.get("vix_symbol", "I:VIX1D")
    if include_vix and vix_symbol:
        try:
            vix = poly.index_history(vix_symbol, start, end)
            if not vix.empty:
                spy["vix"] = vix["close"].reindex(spy.index, method="ffill")
                log.info("Merged real VIX (%s).", vix_symbol)
        except PolygonError as exc:
            log.warning("VIX unavailable (%s). Proceeding without VIX features. %s",
                        vix_symbol, exc)
    if _day_is_complete(end):
        spy.to_parquet(cache)
    return spy


def has_vix(bars: pd.DataFrame) -> bool:
    return "vix" in bars.columns and bars["vix"].notna().any()


def build_training_set(cfg, bars: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    include_vix = has_vix(bars)
    mp = cfg.model_params
    features = build_features(bars, include_vix=include_vix)
    labels, valid = make_labels(
        bars["close"],
        horizon_bars=mp["label"]["horizon_bars"],
        threshold_pct=mp["label"]["threshold_pct"],
    )
    mask = valid & features.notna().all(axis=1)
    X = features[mask]
    y = labels[mask]
    log.info("Training set: %d rows, %.1f%% positive, vix=%s",
             len(X), 100 * y.mean() if len(y) else 0, include_vix)
    return X, y


def build_snapshot(cfg, bars: pd.DataFrame, model=None) -> MarketSnapshot:
    """Build a MarketSnapshot from real bars. Option fields are left to the caller, which
    resolves the real 0DTE contract (Polygon in backtest, IBKR live)."""
    include_vix = has_vix(bars)
    features = build_features(bars, include_vix=include_vix)
    frow = features.iloc[-1]
    price = float(bars["close"].iloc[-1])

    ml_prob = 0.5
    if model is not None:
        ml_prob = model.predict_one(frow.to_dict())

    # Prior 5 bars (exclude the latest) so a breakout of the range is meaningful.
    high5 = float(bars["high"].iloc[-6:-1].max()) if len(bars) > 1 else price
    low5 = float(bars["low"].iloc[-6:-1].min()) if len(bars) > 1 else price
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
        vix=float(frow.get("vix", 0.0)) if include_vix else float("nan"),
        regime=classify_regime(frow),
        ml_prob_up=ml_prob,
        features=frow.to_dict(),
    )


def _main() -> None:
    from ..utils.config import load_config

    parser = argparse.ArgumentParser(description="Data pipeline (real Polygon data)")
    parser.add_argument("--download", action="store_true", help="force fresh download")
    parser.add_argument("--build-training", action="store_true")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    cfg = load_config()
    bars = load_bars(cfg, args.days, download=args.download)
    print(f"Loaded {len(bars)} real SPY bars: {bars.index[0]} -> {bars.index[-1]} "
          f"(vix={'yes' if has_vix(bars) else 'no'})")

    if args.build_training:
        X, y = build_training_set(cfg, bars)
        print(f"X shape: {X.shape}, positive rate: {y.mean():.3f}")


if __name__ == "__main__":
    _main()
