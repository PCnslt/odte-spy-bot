"""Free market data via yfinance.

Limitations you must know:
  * yfinance only serves 1-minute bars for roughly the last 30 days, and only in <=8-day
    chunks. We stitch chunks together. For anything longer you need a paid feed; swap this
    class for one with the same interface.
  * ^VIX 1-minute data is unreliable; we fetch it best-effort and forward-fill onto the SPY
    index, falling back to a constant if it is unavailable.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from ..utils.logger import get_logger

log = get_logger("free_feed")

_COLS = ["open", "high", "low", "close", "volume"]


class YFinanceFeed:
    def __init__(self, symbol: str = "SPY", vix_symbol: str = "^VIX",
                 interval: str = "1m", cache_dir: str | Path = "data"):
        self.symbol = symbol
        self.vix_symbol = vix_symbol
        self.interval = interval
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # --- download --------------------------------------------------------------
    def _download_chunked(self, symbol: str, days: int) -> pd.DataFrame:
        import yfinance as yf

        end = datetime.now()
        frames: list[pd.DataFrame] = []
        # yfinance caps 1m history at ~8 days per call.
        chunk = 7 if self.interval.endswith("m") else 365
        cursor = end
        remaining = days
        while remaining > 0:
            span = min(chunk, remaining)
            start = cursor - timedelta(days=span)
            try:
                df = yf.download(symbol, start=start, end=cursor, interval=self.interval,
                                 progress=False, auto_adjust=False, prepost=False)
            except Exception as exc:
                log.warning("yfinance download failed for %s: %s", symbol, exc)
                df = pd.DataFrame()
            if not df.empty:
                frames.append(df)
            cursor = start
            remaining -= span
        if not frames:
            return pd.DataFrame(columns=_COLS)
        out = pd.concat(frames).sort_index()
        out = out[~out.index.duplicated(keep="first")]
        return self._normalize(out)

    @staticmethod
    def _normalize(df: pd.DataFrame) -> pd.DataFrame:
        # yfinance may return a MultiIndex column frame (ticker level); flatten it.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)
        keep = [c for c in _COLS if c in df.columns]
        df = df[keep].copy()
        df.index = pd.to_datetime(df.index, utc=True)
        return df.dropna(how="all")

    def download(self, days: int = 30) -> pd.DataFrame:
        """Download SPY bars + VIX, merge, cache to parquet, return the merged frame."""
        spy = self._download_chunked(self.symbol, days)
        if spy.empty:
            raise RuntimeError(
                "No SPY data returned by yfinance. Are you offline, or outside the 30-day "
                "1-minute window? Try a smaller --days or a different interval."
            )
        vix = self._download_chunked(self.vix_symbol, days)
        if not vix.empty:
            spy["vix"] = vix["close"].reindex(spy.index, method="ffill")
        else:
            log.warning("VIX unavailable; filling with a flat 18.0 placeholder.")
            spy["vix"] = 18.0
        spy["vix"] = spy["vix"].ffill().bfill().fillna(18.0)

        path = self.cache_path(days)
        spy.to_parquet(path)
        log.info("Saved %d bars to %s", len(spy), path)
        return spy

    def cache_path(self, days: int) -> Path:
        return self.cache_dir / f"{self.symbol.lower()}_{self.interval}_{days}d.parquet"

    def load_cached(self, days: int = 30) -> Optional[pd.DataFrame]:
        path = self.cache_path(days)
        if path.exists():
            return pd.read_parquet(path)
        return None

    def latest_bars(self, lookback_minutes: int = 120) -> pd.DataFrame:
        """Fetch the most recent bars for the live loop (1 day is enough for intraday state)."""
        df = self._download_chunked(self.symbol, days=1)
        vix = self._download_chunked(self.vix_symbol, days=1)
        if not df.empty:
            if not vix.empty:
                df["vix"] = vix["close"].reindex(df.index, method="ffill")
            df["vix"] = df.get("vix", 18.0)
            df["vix"] = df["vix"].ffill().bfill().fillna(18.0)
        return df.tail(lookback_minutes)
