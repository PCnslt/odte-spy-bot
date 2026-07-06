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

    def resolve_spread(self, kind: str, spot: float, expiry: date, width: float,
                       short_otm_pct: float) -> Optional[dict]:
        """Resolve a defined-risk vertical from REAL quoted legs.

        kind: 'bull_put' (short put below spot) or 'bear_call' (short call above spot).
        Returns {kind, short, long, credit, width} where short/long are qualified ib_insync
        Option contracts and credit is estimated from the legs' latest real prices."""
        from ib_insync import Option

        right = "P" if kind == "bull_put" else "C"
        if kind == "bull_put":
            short_strike = float(int(spot * (1 - short_otm_pct)))
            long_strike = short_strike - width
        else:
            short_strike = float(int(spot * (1 + short_otm_pct)) + 1)
            long_strike = short_strike + width

        legs = []
        for strike in (short_strike, long_strike):
            opt = Option(self.symbol, expiry.strftime("%Y%m%d"), strike, right,
                         self.exchange, currency=self.currency)
            try:
                self.ib.qualifyContracts(opt)
            except Exception as exc:
                log.warning("Cannot qualify %s %s %s: %s", right, strike, expiry, exc)
                return None
            legs.append(opt)

        prices = []
        for opt in legs:
            bars = self._hist(opt, "1 D", "1 min")
            if bars.empty:
                log.warning("No bars for leg %s", opt.localSymbol)
                return None
            prices.append(float(bars["close"].iloc[-1]))

        credit = prices[0] - prices[1]   # receive short, pay long
        return {"kind": kind, "short": legs[0], "long": legs[1],
                "short_price": prices[0], "long_price": prices[1],
                "credit": credit, "width": abs(short_strike - long_strike)}

    def overnight_gap(self) -> Optional[float]:
        """(today's open - prior session close) / prior close, from real daily bars.
        None when unavailable. Used by the opening-gap guard: 0DTE positions never survive
        overnight here, but ENTERING into a violent post-gap open is a real risk the
        anomaly detector can't see until it has warmed up."""
        from ib_insync import Stock

        try:
            spy = Stock(self.symbol, self.exchange, self.currency)
            self.ib.qualifyContracts(spy)
            bars = self.ib.reqHistoricalData(
                spy, endDateTime="", durationStr="2 D", barSizeSetting="1 day",
                whatToShow="TRADES", useRTH=True, formatDate=2)
            if len(bars) < 2:
                return None
            prev_close, today_open = float(bars[-2].close), float(bars[-1].open)
            if prev_close <= 0:
                return None
            return (today_open - prev_close) / prev_close
        except Exception as exc:
            log.warning("overnight_gap unavailable: %s", exc)
            return None

    def leg_quotes(self, spread: dict, timeout_s: float = 4.0) -> Optional[dict]:
        """REAL bid/ask for both legs via a market-data snapshot (works with delayed data).
        Returns {short_bid, short_ask, long_bid, long_ask, mid_credit} or None if any side
        is unavailable — callers decide whether that skips the trade (require_quotes)."""
        try:
            tickers = self.ib.reqTickers(spread["short"], spread["long"])
        except Exception as exc:
            log.warning("leg_quotes failed: %s", exc)
            return None
        if len(tickers) != 2:
            return None
        s, l = tickers
        vals = {"short_bid": s.bid, "short_ask": s.ask,
                "long_bid": l.bid, "long_ask": l.ask}
        for k, v in vals.items():
            if v is None or v != v or v < 0:   # NaN or invalid
                log.info("leg_quotes: %s unavailable", k)
                return None
        mid_credit = ((vals["short_bid"] + vals["short_ask"]) / 2
                      - (vals["long_bid"] + vals["long_ask"]) / 2)
        return {**vals, "mid_credit": mid_credit}

    def spread_close_cost(self, spread: dict) -> Optional[float]:
        """Current REAL cost to close the spread (buy back short, sell long)."""
        costs = []
        for opt in (spread["short"], spread["long"]):
            bars = self._hist(opt, "1 D", "1 min")
            if bars.empty:
                return None
            costs.append(float(bars["close"].iloc[-1]))
        return costs[0] - costs[1]

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
