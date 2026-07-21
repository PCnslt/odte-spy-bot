"""P0 2026-07-21 regression: leg_quotes must WAIT for delayed ticks, not read-and-bail.

After the weekend Gateway update, delayed snapshots return before quote ticks land; reading
immediately saw NaN bids and every entry on 07-20/21 was skipped ("leg quotes unavailable").
Ticker objects update in place — leg_quotes now polls them up to timeout_s.
"""
from __future__ import annotations

import math

from src.data.ibkr_feed import IBKRFeed


class _Ticker:
    """Delayed ticker: NaN until `after` pumps of ib.sleep, then real values."""
    def __init__(self, bid, ask, after=0):
        self._final, self._after = (bid, ask), after
        self.bid = self.ask = math.nan
        if after == 0:
            self.bid, self.ask = bid, ask

    def _tick(self):
        if self._after > 0:
            self._after -= 1
            if self._after == 0:
                self.bid, self.ask = self._final


class _FakeIB:
    def __init__(self, tickers):
        self._tickers = tickers
        self.sleeps = 0

    def reqTickers(self, *contracts):
        return self._tickers

    def sleep(self, *_a):
        self.sleeps += 1
        for t in self._tickers:
            t._tick()


def _feed(tickers):
    f = IBKRFeed.__new__(IBKRFeed)     # leg_quotes touches only self.ib
    f.ib = _FakeIB(tickers)
    return f


SPREAD = {"short": object(), "long": object()}


def test_immediate_quotes_still_work():
    f = _feed([_Ticker(0.50, 0.55), _Ticker(0.20, 0.25)])
    q = f.leg_quotes(SPREAD)
    assert q is not None and abs(q["mid_credit"] - 0.30) < 1e-9
    assert f.ib.sleeps == 0                      # no pointless waiting when data is there


def test_late_delayed_ticks_are_awaited():
    """The 07-20/21 failure mode: NaN at return, real values a few pumps later."""
    f = _feed([_Ticker(0.50, 0.55, after=3), _Ticker(0.20, 0.25, after=2)])
    q = f.leg_quotes(SPREAD, timeout_s=2.0)
    assert q is not None and q["short_bid"] == 0.50
    assert f.ib.sleeps >= 3                      # it actually waited for the ticks


def test_never_arriving_quote_returns_none():
    f = _feed([_Ticker(0.50, 0.55), _Ticker(math.nan, math.nan, after=10_000)])
    assert f.leg_quotes(SPREAD, timeout_s=0.3) is None   # fail-closed, bounded wait
