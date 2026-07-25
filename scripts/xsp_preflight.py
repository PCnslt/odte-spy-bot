"""XSP rehearsal preflight — READ-ONLY, no orders. Run Monday 09:00 before the rehearsal.

    venv/bin/python scripts/xsp_preflight.py

PASS on every line = start the rehearsal (docs/XSP_REHEARSAL_RUNBOOK.md). Any FAIL = don't.
"""
from __future__ import annotations

import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn):
    try:
        detail = fn()
        RESULTS.append((name, True, str(detail)))
    except Exception as exc:
        RESULTS.append((name, False, str(exc)[:140]))


def main() -> int:
    from ib_insync import IB, Index, Option

    ib = IB()
    check("gateway auth", lambda: (ib.connect("127.0.0.1", 4002, clientId=59, timeout=20),
                                   ib.managedAccounts())[1])
    ib.reqMarketDataType(3)

    und = Index("XSP", "CBOE", "USD")
    check("XSP index qualifies", lambda: ib.qualifyContracts(und)[0].conId)

    def _spot():
        [t] = ib.reqTickers(und)
        ib.sleep(2)
        s = t.last or t.close
        if not s or s != s:
            raise RuntimeError("no XSP spot")
        return round(float(s), 2)
    check("XSP delayed spot", _spot)

    def _chain():
        [t] = ib.reqTickers(und)
        ib.sleep(1)
        s = float(t.last or t.close)
        expiry = datetime.now().strftime("%Y%m%d")
        opts = [Option("XSP", expiry, round(s) + d, r, "SMART", currency="USD",
                       tradingClass="XSP") for d in (-2, -1, 0, 1, 2) for r in ("P", "C")]
        q = [o for o in ib.qualifyContracts(*opts) if o.conId]
        if len(q) < 6:
            raise RuntimeError(f"only {len(q)}/10 qualified")
        return f"{len(q)}/10 near-money 0DTE contracts qualify (tradingClass XSP)"
    check("XSP 0DTE chain qualifies", _chain)

    def _quote():
        [t] = ib.reqTickers(und)
        ib.sleep(1)
        s = float(t.last or t.close)
        expiry = datetime.now().strftime("%Y%m%d")
        [opt] = ib.qualifyContracts(Option("XSP", expiry, round(s), "P", "SMART",
                                           currency="USD", tradingClass="XSP"))
        [tk] = ib.reqTickers(opt)
        deadline = 8
        while deadline and (tk.bid != tk.bid or tk.ask != tk.ask):
            ib.sleep(1)
            deadline -= 1
        if tk.bid != tk.bid or tk.ask != tk.ask:
            raise RuntimeError("no two-sided delayed quote after 8s")
        return f"ATM put {tk.bid}/{tk.ask}"
    check("XSP delayed option quote", _quote)

    def _polygon():
        key = ""
        for ln in Path(".env").read_text().splitlines():
            if ln.startswith("POLYGON_API_KEY="):
                key = ln.split("=", 1)[1].strip()
        if not key:
            raise RuntimeError("no POLYGON_API_KEY in .env")
        url = ("https://api.polygon.io/v3/reference/options/contracts?underlying_ticker=XSP"
               f"&limit=1&apiKey={key}")
        with urllib.request.urlopen(url, timeout=15) as r:
            import json
            n = len(json.load(r).get("results", []) or [])
        if n < 1:
            raise RuntimeError("polygon returned no XSP contracts")
        return "XSP contracts visible"
    check("polygon XSP reachable", _polygon)

    try:
        ib.disconnect()
    except Exception:
        pass

    ok = all(p for _, p, _ in RESULTS)
    for name, p, detail in RESULTS:
        print(f"{'PASS' if p else 'FAIL'}  {name}: {detail}")
    print(f"\nPREFLIGHT {'PASS — start the rehearsal' if ok else 'FAIL — do NOT start'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
