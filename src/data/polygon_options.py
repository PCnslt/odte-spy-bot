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
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from ..utils.logger import get_logger

log = get_logger("polygon")

_ET = ZoneInfo("America/New_York")


def _now_et() -> datetime:
    return datetime.now(_ET)


def _day_is_complete(day: date) -> bool:
    """True when `day`'s session data can no longer change (past date, weekend, or today
    after 16:15 ET). Guards the cache against poisoning by partial same-day fetches
    (audit C2): an intraday fetch cached as 'the full day' would corrupt every later
    retrain silently."""
    now = _now_et()
    if day < now.date() or day.weekday() >= 5:  # past, or Sat/Sun (no session to corrupt)
        return True
    return day == now.date() and (now.hour, now.minute) >= (16, 15)


def _cache_fresh(path: Path, day: date) -> bool:
    """A cached file whose range ends on `day` is only trustworthy if it was WRITTEN after
    that day's close — otherwise it may hold a partial session from an intraday run."""
    if not path.exists():
        return False
    if day.weekday() >= 5:  # weekend end-date: nothing was trading; any write is complete
        return True
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=_ET)
    close = datetime(day.year, day.month, day.day, 16, 15, tzinfo=_ET)
    return mtime >= close


class PolygonError(RuntimeError):
    pass


def compute_gex(rows: list[dict]) -> Optional[dict]:
    """Aggregate a chain snapshot into naive GEX + surface features. Pure (unit-tested).

    Per contract: gamma x open_interest x 100 x spot, calls +, puts -.
    Returns None if nothing usable, else:
      gex_net     - naive $ dealer gamma per 1% move (calls +, puts -)
      gamma_wall  - strike with the largest |gamma x OI| (pin magnet)
      atm_iv      - IV of the call+put nearest spot, averaged
      skew_25d    - 25-delta risk reversal: IV(put ~ -0.25 delta) - IV(call ~ +0.25 delta);
                    positive = downside fear priced richer (the usual index state)
      n_used      - contracts with usable greeks
    All market-implied, no Black-Scholes. Off-hours the snapshot lacks greeks -> None."""
    net = 0.0
    wall_strike, wall_mass = None, 0.0
    n_used = 0
    atm_call_iv = atm_put_iv = None
    atm_call_d = atm_put_d = float("inf")
    # nearest-to-target-delta trackers for the 25-delta skew
    put_iv_25 = call_iv_25 = None
    put_25_err = call_25_err = float("inf")
    for c in rows or []:
        greeks = c.get("greeks") or {}
        gamma = greeks.get("gamma")
        delta = greeks.get("delta")
        iv = c.get("implied_volatility")
        oi = c.get("open_interest") or 0
        det = c.get("details") or {}
        strike = det.get("strike_price")
        ctype = det.get("contract_type")
        spot = (c.get("underlying_asset") or {}).get("price")
        if gamma is None or not oi or strike is None or ctype not in ("call", "put") \
                or not spot:
            continue
        sign = 1.0 if ctype == "call" else -1.0
        net += sign * float(gamma) * float(oi) * 100.0 * float(spot)
        mass = abs(float(gamma) * float(oi))
        if mass > wall_mass:
            wall_mass, wall_strike = mass, float(strike)
        n_used += 1
        # ATM IV: closest strike to spot on each side
        dist = abs(float(strike) - float(spot))
        if iv is not None:
            if ctype == "call" and dist < atm_call_d:
                atm_call_d, atm_call_iv = dist, float(iv)
            if ctype == "put" and dist < atm_put_d:
                atm_put_d, atm_put_iv = dist, float(iv)
            # 25-delta skew: put near -0.25, call near +0.25
            if delta is not None:
                if ctype == "put" and abs(abs(float(delta)) - 0.25) < put_25_err:
                    put_25_err, put_iv_25 = abs(abs(float(delta)) - 0.25), float(iv)
                if ctype == "call" and abs(abs(float(delta)) - 0.25) < call_25_err:
                    call_25_err, call_iv_25 = abs(abs(float(delta)) - 0.25), float(iv)
    if n_used == 0:
        return None
    atm_iv = None
    ivs = [v for v in (atm_call_iv, atm_put_iv) if v is not None]
    if ivs:
        atm_iv = sum(ivs) / len(ivs)
    skew_25d = (put_iv_25 - call_iv_25) if (put_iv_25 is not None
                                            and call_iv_25 is not None) else None
    return {"gex_net": net, "gamma_wall": wall_strike, "atm_iv": atm_iv,
            "skew_25d": skew_25d, "n_used": n_used}


def prob_touch(delta: Optional[float]) -> Optional[float]:
    """Market-implied probability of the underlying TOUCHING a strike before expiry, from
    that strike's option delta. |delta| ~ risk-neutral P(finish ITM); P(touch) ~ 2 x that
    (reflection principle for a barrier), capped at 1. This is the spread-seller's core
    risk number, taken from real market prices rather than a Black-Scholes assumption."""
    if delta is None:
        return None
    return min(2.0 * abs(float(delta)), 1.0)


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
        """Real minute bars for a single option contract on `day`. Cached ONLY once the
        session is complete; a same-day intraday request is served fresh and never cached
        (audit C2: partial-day cache poisoning)."""
        cache = self.cache_dir / "options" / f"{ticker.replace(':', '_')}_{day:%Y%m%d}.parquet"
        if cache.exists() and _cache_fresh(cache, day):
            return pd.read_parquet(cache)
        df = self._aggs(ticker, f"{day:%Y-%m-%d}", f"{day:%Y-%m-%d}")
        if _day_is_complete(day):
            df.to_parquet(cache)
        else:
            log.info("option_bars(%s, %s): session incomplete — served fresh, not cached.",
                     ticker, day)
        return df

    def stock_history(self, start: date, end: date, symbol: str = "SPY") -> pd.DataFrame:
        """Real SPY minute bars over [start, end]. Same completeness guard as option_bars:
        a range that includes an unfinished session is never cached."""
        cache = self.cache_dir / f"{symbol.lower()}_1m_{start:%Y%m%d}_{end:%Y%m%d}.parquet"
        if cache.exists() and _cache_fresh(cache, end):
            return pd.read_parquet(cache)
        df = self._aggs(symbol, f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}")
        if _day_is_complete(end):
            df.to_parquet(cache)
        return df

    def gex_snapshot(self, expiry: date, underlying: str = "SPY") -> Optional[dict]:
        """Naive 0DTE gamma-exposure snapshot from the REAL chain (R10 instrumentation).

        Uses the live chain snapshot (Starter-entitled): per contract, gamma x OI x 100 x
        spot, calls positive / puts negative (the standard naive dealer-positioning
        convention). Also reports the 'gamma wall' (strike with the largest absolute
        gamma x OI). Contracts without greeks (deep ITM/OTM) are skipped — they carry
        ~zero gamma. Telemetry only until H7's pre-registered test; fail-safe None."""
        try:
            rows = self._get(f"{self.base_url}/v3/snapshot/options/{underlying}",
                             {"expiration_date": f"{expiry:%Y-%m-%d}", "limit": 250})
        except Exception as exc:
            log.info("gex_snapshot unavailable: %s", exc)
            return None
        return compute_gex(rows)

    def contract_snapshot(self, option_ticker: str, underlying: str = "SPY") -> dict:
        """Live per-contract IV + greeks from the snapshot (Starter-entitled). Present-time
        only (no IV/greeks history on this plan — the reason the TradeLog records them at
        entry). Returns {'iv','delta','gamma'} with None for any unavailable field; all
        None off-hours or for illiquid strikes (fail-safe)."""
        try:
            resp = self._request(
                f"{self.base_url}/v3/snapshot/options/{underlying}/{option_ticker}",
                {"apiKey": self.api_key})
            r = resp.json().get("results") or {}
            g = r.get("greeks") or {}
            def _f(x):
                return float(x) if x is not None else None
            return {"iv": _f(r.get("implied_volatility")), "delta": _f(g.get("delta")),
                    "gamma": _f(g.get("gamma"))}
        except Exception as exc:
            log.info("contract_snapshot unavailable for %s: %s", option_ticker, exc)
            return {"iv": None, "delta": None, "gamma": None}

    def current_iv(self, option_ticker: str, underlying: str = "SPY") -> Optional[float]:
        """Back-compat thin wrapper around contract_snapshot -> IV only."""
        return self.contract_snapshot(option_ticker, underlying).get("iv")

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
