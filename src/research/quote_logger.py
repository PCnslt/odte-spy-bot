"""Free NBBO archive builder — records the DELAYED option-chain quotes IBKR already serves.

The insight: marketDataType=3 (free, no subscription) delivers REAL bid/ask, just 15 minutes
late. Delay is irrelevant for an archive — historical is historical. Logging the near-the-money
XSP 0DTE chain every minute during sessions builds, at $0, the three things Master Plan v3
needs: (a) a genuine quote archive for forward validation, (b) a width(delta, time-of-day)
calibration for the G1.5 screening backtest's fill model, (c) the G4 measurement of XSP
execution quality. It never fabricates a quote — rows are written only when both sides exist.

Standalone, read-only, fail-soft: own client id, crashes never touch the trading loop.
    python -m src.research.quote_logger              # runs until 16:05 ET, then exits
"""
from __future__ import annotations

import csv
import gzip
import time
from datetime import datetime, time as dtime
from pathlib import Path

from ..utils.config import load_config
from ..utils.logger import get_logger

log = get_logger("quote_logger")

CLIENT_ID = 49          # unique: live 17/18/22, reconcile 47/147, flatten 48, dashboard 57/157
CADENCE_S = 60.0        # one sweep per minute — pacing-safe for ~60 snapshot tickers
WINDOW_PCT = 0.02       # strikes within ±2% of spot
STOP_AT = dtime(16, 5)
OUT_DIR = Path("logs/quotes")


def strike_window(spot: float, pct: float = WINDOW_PCT, step: float = 1.0) -> list[float]:
    """Strikes within ±pct of spot on a fixed grid (XSP strikes are $1 apart)."""
    if spot <= 0 or pct <= 0 or step <= 0:
        return []
    lo = int((spot * (1 - pct)) // step) * step
    hi = (int((spot * (1 + pct)) // step) + 1) * step
    n = int(round((hi - lo) / step)) + 1
    return [round(lo + i * step, 2) for i in range(n)]


def quote_row(ts: str, symbol: str, expiry: str, strike: float, right: str,
              bid, ask, bid_size, ask_size) -> list | None:
    """CSV row, or None when either side is missing/crossed — never a fabricated quote."""
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if b != b or a != a or b < 0 or a <= 0 or a < b:   # NaN-safe, crossed-safe
        return None
    return [ts, symbol, expiry, strike, right, round(b, 4), round(a, 4),
            int(bid_size or 0), int(ask_size or 0)]


def run(symbol: str = "XSP", underlying_symbol: str = "XSP") -> None:   # pragma: no cover
    from ib_insync import IB, Index, Option

    cfg = load_config()
    ib_cfg = cfg.execution.ibkr
    ib = IB()
    ib.connect(ib_cfg.host, ib_cfg.paper_port, clientId=CLIENT_ID, timeout=25)
    ib.reqMarketDataType(3)                            # the free delayed feed — the whole point
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    out = OUT_DIR / f"{day}_{symbol.lower()}.csv.gz"
    new = not out.exists()
    fh = gzip.open(out, "at", newline="")
    w = csv.writer(fh)
    if new:
        w.writerow(["ts", "symbol", "expiry", "strike", "right", "bid", "ask",
                    "bid_size", "ask_size"])

    und = Index(underlying_symbol, "CBOE", "USD")
    ib.qualifyContracts(und)
    expiry = datetime.now().strftime("%Y%m%d")         # 0DTE chain
    log.info("quote_logger: %s 0DTE chain every %ds -> %s", symbol, int(CADENCE_S), out)

    kept = 0
    while datetime.now().time() < STOP_AT:
        try:
            [t_und] = ib.reqTickers(und)
            spot = t_und.last or t_und.close
            if not spot or spot != spot:
                ib.sleep(CADENCE_S)
                continue
            opts = [Option(symbol, expiry, k, r, "SMART", currency="USD")
                    for k in strike_window(float(spot)) for r in ("P", "C")]
            opts = [o for o in ib.qualifyContracts(*opts) if o.conId]
            ts = datetime.now().isoformat(timespec="seconds")
            for tk in ib.reqTickers(*opts):
                row = quote_row(ts, symbol, expiry, tk.contract.strike, tk.contract.right,
                                tk.bid, tk.ask, tk.bidSize, tk.askSize)
                if row:
                    w.writerow(row)
                    kept += 1
            fh.flush()
        except Exception as exc:                       # fail-soft: log and keep sweeping
            log.warning("quote sweep failed: %s", exc)
        ib.sleep(CADENCE_S)

    fh.close()
    ib.disconnect()
    log.info("quote_logger done: %d quote rows archived.", kept)


if __name__ == "__main__":                             # pragma: no cover
    run()
