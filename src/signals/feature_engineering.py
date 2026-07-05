"""Feature engineering. Pure, vectorized, and strictly causal (no look-ahead).

Every feature at bar t uses only information available at or before t. We compute an intraday
VWAP that resets each session and realized-volatility proxies (free data has no real IV/greeks
or options-flow, so those columns are proxied and clearly named `*_proxy`).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# The columns the model trains/predicts on. Keep this list authoritative.
FEATURE_COLUMNS = [
    "ret_1", "ret_5", "ret_15",
    "vwap_dev", "rvol", "vol_z",
    "atr_5", "atr_15", "rv_5_proxy", "rv_annual_proxy",
    "rsi_14", "macd", "macd_signal", "macd_hist",
    "ema_9_21_spread", "ema_slope",
    "vix", "vix_change",
    "high_dist_5", "low_dist_5",
    "minutes_into_session",
]


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def _session_vwap(df: pd.DataFrame) -> pd.Series:
    day = df.index.tz_convert("America/New_York").date if df.index.tz else df.index.date
    grp = pd.Series(day, index=df.index)
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (typical * df["volume"]).groupby(grp).cumsum()
    vv = df["volume"].groupby(grp).cumsum().replace(0, np.nan)
    return (pv / vv).ffill()


def _minutes_into_session(index: pd.DatetimeIndex) -> pd.Series:
    local = index.tz_convert("America/New_York") if index.tz else index
    minutes = (local.hour - 9) * 60 + local.minute - 30
    return pd.Series(np.clip(minutes, 0, 390), index=index).astype(float)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Return a feature DataFrame aligned to `df.index`. Input needs OHLCV (+ optional vix)."""
    out = pd.DataFrame(index=df.index)
    close = df["close"]

    out["ret_1"] = close.pct_change(1)
    out["ret_5"] = close.pct_change(5)
    out["ret_15"] = close.pct_change(15)

    vwap = _session_vwap(df)
    out["vwap_dev"] = (close - vwap) / vwap

    vol_ma = df["volume"].rolling(20, min_periods=5).mean()
    out["rvol"] = (df["volume"] / vol_ma.replace(0, np.nan)).fillna(1.0)
    vol_std = df["volume"].rolling(20, min_periods=5).std()
    out["vol_z"] = ((df["volume"] - vol_ma) / vol_std.replace(0, np.nan)).fillna(0.0)

    out["atr_5"] = _atr(df, 5)
    out["atr_15"] = _atr(df, 15)
    rv5 = out["ret_1"].rolling(5, min_periods=2).std()
    out["rv_5_proxy"] = rv5.fillna(0.0)
    out["rv_annual_proxy"] = (rv5 * np.sqrt(252 * 390)).fillna(0.20)  # IV proxy for pricing

    out["rsi_14"] = _rsi(close, 14)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    out["macd"] = macd
    out["macd_signal"] = signal
    out["macd_hist"] = macd - signal

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    out["ema_9_21_spread"] = (ema9 - ema21) / close
    out["ema_slope"] = ema9.pct_change(3)

    if "vix" in df.columns:
        out["vix"] = df["vix"]
        out["vix_change"] = df["vix"].pct_change(5)
    else:
        out["vix"] = 18.0
        out["vix_change"] = 0.0

    high5 = df["high"].rolling(5, min_periods=1).max()
    low5 = df["low"].rolling(5, min_periods=1).min()
    out["high_dist_5"] = (high5 - close) / close
    out["low_dist_5"] = (close - low5) / close

    out["minutes_into_session"] = _minutes_into_session(df.index)

    return out[FEATURE_COLUMNS].replace([np.inf, -np.inf], np.nan).fillna(0.0)
