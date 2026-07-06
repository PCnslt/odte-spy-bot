"""Polygon.io client for REAL SPY + 0DTE option data (backtests).

Verified against Polygon (now docs at massive.com; host still api.polygon.io), 2026:
  * Contracts : GET /v3/reference/options/contracts  (underlying_ticker, expiration_date,
                as_of, expired, contract_type, limit<=1000, paginate via next_url)
  * Aggregates: GET /v2/aggs/ticker/{opt}/range/1/minute/{from}/{to}  (t=epoch ms, limit<=50000)
  * Option ticker: O:SPY{YYMMDD}{C|P}{strike*1000:08d}   e.g. O:SPY260702C00580000

Plans: Options Starter ($29) covers 2yr historical aggregates (what the backtest needs).
NBBO quotes (use_quotes:true) need Options Developer+. VIX1D needs an Indices entitlement.
Everything returned here is real traded data — no modeling.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from ..utils.logger import get_logger

log = get_logger("polygon")


class PolygonError(RuntimeError):
    pass


class PolygonOptions:
    def __init__(self, api_key: str, base_url: str = "https://api.polygon.io",
                 cache_dir: str | Path = "data", rate_limit_per_min: int = 0):
        if not api_key:
            raise PolygonError(
                "POLYGON_API_KEY is not set. Real backtests need a Polygon Options plan. "
                "Put the key in .env (see .env.example)."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.cache_dir = Path(cache_dir)
        (self.cache_dir / "options").mkdir(parents=True, exist_ok=True)
        self.rate_limit_per_min = rate_limit_per_min
        self._session = requests.Session()
        self._last_call = 0.0

    # --- low-level -------------------------------------------------------------
    def _throttle(self) -> None:
        if self.rate_limit_per_min and self.rate_limit_per_min > 0:
            min_gap = 60.0 / self.rate_limit_per_min
            wait = min_gap - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
        self._last_call = time.time()

    def _request(self, url: str, params: Optional[dict], max_retries: int = 6):
        """Single GET with 429 backoff (honors Retry-After) and hard-fail on entitlement errors."""
        for attempt in range(max_retries):
            self._throttle()
            resp = self._session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 0)) or min(5 * 2 ** attempt, 60)
                log.warning("Polygon 429 rate-limited; sleeping %.0fs (attempt %d/%d)",
                            wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            if resp.status_code in (401, 403):
                raise PolygonError(
                    f"Polygon {resp.status_code} for {resp.url.split('apiKey')[0]} — your plan "
                    f"likely lacks this entitlement (e.g. quotes need Developer+, VIX needs "
                    f"Indices). Response: {resp.text[:200]}"
                )
            if resp.status_code != 200:
                raise PolygonError(f"Polygon {resp.status_code}: {resp.text[:300]}")
            return resp
        raise PolygonError("Polygon 429: exhausted retries. Lower data.polygon.rate_limit_per_min.")

    def _get(self, url: str, params: Optional[dict] = None) -> list[dict]:
        """GET with pagination via next_url. Returns the concatenated `results` list."""
        params = dict(params or {})
        params["apiKey"] = self.api_key
        results: list[dict] = []
        next_url: Optional[str] = url
        next_params: Optional[dict] = params
        while next_url:
            resp = self._request(next_url, next_params)
            body = resp.json()
            results.extend(body.get("results", []) or [])
            nxt = body.get("next_url")
            # next_url already carries the query; only the apiKey must be re-appended.
            next_url = nxt
            next_params = {"apiKey": self.api_key} if nxt else None
        return results

    # --- reference -------------------------------------------------------------
    @staticmethod
    def option_ticker(strike: float, right: str, expiry: date) -> str:
        """Build the OCC/Polygon option ticker. `right` in {'C','P'}."""
        return f"O:SPY{expiry:%y%m%d}{right}{int(round(strike * 1000)):08d}"

    def list_contracts(self, expiration: date, underlying: str = "SPY") -> pd.DataFrame:
        """All contracts expiring on `expiration` as they existed that day (real chain)."""
        cache = self.cache_dir / "options" / f"chain_{underlying}_{expiration:%Y%m%d}.parquet"
        if cache.exists():
            return pd.read_parquet(cache)
        rows = self._get(
            f"{self.base_url}/v3/reference/options/contracts",
            {"underlying_ticker": underlying, "expiration_date": f"{expiration:%Y-%m-%d}",
             "as_of": f"{expiration:%Y-%m-%d}", "expired": "true", "limit": 1000},
        )
        if not rows:
            df = pd.DataFrame(columns=["ticker", "strike", "type"])
        else:
            df = pd.DataFrame([{
                "ticker": r["ticker"], "strike": r["strike_price"],
                "type": "C" if r["contract_type"] == "call" else "P",
            } for r in rows]).sort_values("strike").reset_index(drop=True)
        df.to_parquet(cache)
        return df

    def nearest_contract(self, expiration: date, right: str, spot: float,
                         strike_offset: int = 0) -> Optional[dict]:
        """Pick the real listed strike nearest `spot` for the given right, +/- offset strikes."""
        chain = self.list_contracts(expiration)
        side = chain[chain["type"] == right].reset_index(drop=True)
        if side.empty:
            return None
        idx = (side["strike"] - spot).abs().idxmin()
        idx = int(min(max(idx + strike_offset, 0), len(side) - 1))
        row = side.iloc[idx]
        return {"ticker": row["ticker"], "strike": float(row["strike"]), "type": right}

    # --- aggregates ------------------------------------------------------------
    def _aggs(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        rows = self._get(
            f"{self.base_url}/v2/aggs/ticker/{ticker}/range/1/minute/{start}/{end}",
            {"adjusted": "true", "sort": "asc", "limit": 50000},
        )
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "vwap"])
        df = pd.DataFrame(rows)
        df["ts"] = pd.to_datetime(df["t"], unit="ms", utc=True)
        df = df.set_index("ts")
        out = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close",
                                 "v": "volume", "vw": "vwap"})
        keep = [c for c in ["open", "high", "low", "close", "volume", "vwap"] if c in out]
        return out[keep]

    def option_bars(self, ticker: str, day: date) -> pd.DataFrame:
        """Real minute bars for a single option contract on `day` (cached)."""
        cache = self.cache_dir / "options" / f"{ticker.replace(':', '_')}_{day:%Y%m%d}.parquet"
        if cache.exists():
            return pd.read_parquet(cache)
        df = self._aggs(ticker, f"{day:%Y-%m-%d}", f"{day:%Y-%m-%d}")
        df.to_parquet(cache)
        return df

    def stock_history(self, start: date, end: date, symbol: str = "SPY") -> pd.DataFrame:
        """Real SPY minute bars over [start, end] (cached per range)."""
        cache = self.cache_dir / f"{symbol.lower()}_1m_{start:%Y%m%d}_{end:%Y%m%d}.parquet"
        if cache.exists():
            return pd.read_parquet(cache)
        df = self._aggs(symbol, f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}")
        df.to_parquet(cache)
        return df

    def current_iv(self, option_ticker: str, underlying: str = "SPY") -> Optional[float]:
        """Implied volatility of a contract from the LIVE snapshot (Starter-entitled).
        Present-time only — Polygon has no IV history on this plan, which is exactly why
        the TradeLog records it at every entry. Returns None if unavailable."""
        try:
            resp = self._request(
                f"{self.base_url}/v3/snapshot/options/{underlying}/{option_ticker}",
                {"apiKey": self.api_key})
            iv = (resp.json().get("results") or {}).get("implied_volatility")
            return float(iv) if iv is not None else None
        except Exception as exc:
            log.info("current_iv unavailable for %s: %s", option_ticker, exc)
            return None

    def index_history(self, index_ticker: str, start: date, end: date) -> pd.DataFrame:
        """Real index minute bars, e.g. I:VIX1D (needs an Indices entitlement)."""
        return self._aggs(index_ticker, f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}")

    @classmethod
    def from_config(cls, cfg) -> "PolygonOptions":
        p = cfg.data.polygon
        return cls(
            api_key=cfg.secrets.get("polygon_api_key", ""),
            base_url=p.get("base_url", "https://api.polygon.io"),
            cache_dir=cfg.data.get("cache_dir", "data"),
            rate_limit_per_min=p.get("rate_limit_per_min", 0),
        )
