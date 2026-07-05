"""Optional live market data via Interactive Brokers (ib_insync).

Requires TWS or IB Gateway running with the API enabled, and a market-data subscription for
SPY/index data (IBKR delayed data works for testing: it falls back automatically). Import of
ib_insync is lazy so the rest of the project runs without it installed.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from ..utils.logger import get_logger

log = get_logger("ibkr_feed")


class IBKRFeed:
    def __init__(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 18,
                 symbol: str = "SPY"):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.symbol = symbol
        self.ib = None

    def connect(self) -> bool:
        try:
            from ib_insync import IB
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("ib_insync not installed. pip install -r requirements-extras.txt") from exc
        from ib_insync import IB

        self.ib = IB()
        try:
            self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=10)
            # Use delayed data if the account has no live subscription.
            self.ib.reqMarketDataType(3)
            log.info("IBKR feed connected on %s:%d", self.host, self.port)
            return True
        except Exception as exc:
            log.error("IBKR feed connection failed: %s", exc)
            return False

    def latest_bars(self, lookback_minutes: int = 120) -> pd.DataFrame:
        from ib_insync import Stock, Index

        spy = Stock(self.symbol, "SMART", "USD")
        bars = self.ib.reqHistoricalData(
            spy, endDateTime="", durationStr="1 D", barSizeSetting="1 min",
            whatToShow="TRADES", useRTH=True, formatDate=2,
        )
        df = self._bars_to_df(bars)
        try:
            vix = Index("VIX", "CBOE", "USD")
            vbars = self.ib.reqHistoricalData(
                vix, endDateTime="", durationStr="1 D", barSizeSetting="1 min",
                whatToShow="TRADES", useRTH=True, formatDate=2,
            )
            vdf = self._bars_to_df(vbars)
            df["vix"] = vdf["close"].reindex(df.index, method="ffill")
        except Exception as exc:
            log.warning("VIX from IBKR unavailable (%s); using 18.0", exc)
            df["vix"] = 18.0
        df["vix"] = df["vix"].ffill().bfill().fillna(18.0)
        return df.tail(lookback_minutes)

    @staticmethod
    def _bars_to_df(bars) -> pd.DataFrame:
        rows = [{
            "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume,
        } for b in bars]
        idx = pd.to_datetime([b.date for b in bars], utc=True)
        return pd.DataFrame(rows, index=idx)

    def disconnect(self) -> None:
        if self.ib is not None:
            self.ib.disconnect()
