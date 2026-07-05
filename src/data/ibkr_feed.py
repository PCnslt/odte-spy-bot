"""Real-time market data via Interactive Brokers (ib_insync) for the live loop.

Requires TWS or IB Gateway running with the API enabled and a real-time market-data
subscription covering SPY and SPY options (IBKR delayed data works for testing — we request
type 3 as a fallback). ib_insync is imported lazily.

Everything returned here is real. If VIX is unavailable we DROP it rather than fabricate a
value, so features stay honest.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd

from ..utils.logger import get_logger

log = get_logger("ibkr_feed")


class IBKRFeed:
    def __init__(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 18,
                 symbol: str = "SPY", exchange: str = "SMART", currency: str = "USD"):
        self.host, self.port, self.client_id = host, port, client_id
        self.symbol, self.exchange, self.currency = symbol, exchange, currency
        self.ib = None

    def connect(self) -> bool:
        try:
            from ib_insync import IB
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("ib_insync not installed. pip install -r requirements-extras.txt") from exc
        from ib_insync import IB

        self.ib = IB()
        self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=15)
        self.ib.reqMarketDataType(3)  # delayed-frozen fallback if no live entitlement
        log.info("IBKR feed connected on %s:%d", self.host, self.port)
        return True

    # --- underlying ------------------------------------------------------------
    def latest_bars(self, lookback_minutes: int = 120) -> pd.DataFrame:
        from ib_insync import Index, Stock

        spy = Stock(self.symbol, self.exchange, self.currency)
        self.ib.qualifyContracts(spy)
        df = self._hist(spy, "1 D", "1 min")
        try:
            vix = Index("VIX1D", "CBOE", self.currency)
            self.ib.qualifyContracts(vix)
            vdf = self._hist(vix, "1 D", "1 min")
            if not vdf.empty:
                df["vix"] = vdf["close"].reindex(df.index, method="ffill")
        except Exception as exc:
            log.warning("VIX unavailable from IBKR (%s); proceeding without VIX.", exc)
        return df.tail(lookback_minutes)

    # --- options ---------------------------------------------------------------
    def resolve_option(self, right: str, spot: float, expiry: date,
                       strike_offset: int = 0) -> Optional[dict]:
        """Pick the nearest $1 strike, fetch its real last price + ATR from IBKR.

        Returns {strike, right, entry_price, atr, label} or None if no data."""
        from ib_insync import Option

        strike = float(round(spot)) + strike_offset
        opt = Option(self.symbol, expiry.strftime("%Y%m%d"), strike, right,
                     self.exchange, currency=self.currency)
        try:
            self.ib.qualifyContracts(opt)
        except Exception as exc:
            log.warning("Could not qualify option %s %s: %s", strike, right, exc)
            return None

        obars = self._hist(opt, "1 D", "1 min")
        if obars.empty:
            return None
        last = float(obars["close"].iloc[-1])
        atr = self._atr(obars, 10)
        label = f"SPY {expiry:%y%m%d} {right}{strike:g}"
        return {"strike": strike, "right": right, "entry_price": last, "atr": atr,
                "label": label}

    # --- helpers ---------------------------------------------------------------
    def _hist(self, contract, duration: str, bar_size: str) -> pd.DataFrame:
        bars = self.ib.reqHistoricalData(
            contract, endDateTime="", durationStr=duration, barSizeSetting=bar_size,
            whatToShow="TRADES", useRTH=True, formatDate=2,
        )
        if not bars:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        idx = pd.to_datetime([b.date for b in bars], utc=True)
        return pd.DataFrame(
            [{"open": b.open, "high": b.high, "low": b.low, "close": b.close,
              "volume": b.volume} for b in bars], index=idx)

    @staticmethod
    def _atr(bars: pd.DataFrame, n: int) -> float:
        if len(bars) < 2:
            return 0.0
        h = bars.tail(n + 1)
        prev = h["close"].shift(1)
        tr = pd.concat([h["high"] - h["low"], (h["high"] - prev).abs(),
                        (h["low"] - prev).abs()], axis=1).max(axis=1)
        return float(tr.iloc[1:].mean())

    def disconnect(self) -> None:
        if self.ib is not None:
            self.ib.disconnect()
