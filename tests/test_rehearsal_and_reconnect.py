"""P0 feed-reconnect + XSP rehearsal wiring (Section 10 / runbook).

Covers: the 2026-07-24 'alive but disconnected' fix (feed.reconnect + feed_state mirror),
Index-vs-Stock underlying selection, tradingClass passthrough, and the --rehearsal config
overrides that keep rehearsal fills out of the gate-evidence book.
"""
from __future__ import annotations

import json

from src.data.ibkr_feed import IBKRFeed
from src.main import REHEARSAL_RISK_STATE, apply_rehearsal, write_feed_state
from src.utils.config import load_config


# --- feed reconnect -------------------------------------------------------------------------
def test_reconnect_rebuilds_session(monkeypatch):
    f = IBKRFeed.__new__(IBKRFeed)
    calls = []

    class _DeadIB:
        def disconnect(self):
            calls.append("disconnect")
    f.ib = _DeadIB()
    monkeypatch.setattr(IBKRFeed, "connect", lambda self: calls.append("connect") or True)
    assert f.reconnect() is True
    assert calls == ["disconnect", "connect"]


def test_reconnect_fail_soft(monkeypatch):
    f = IBKRFeed.__new__(IBKRFeed)
    f.ib = None
    monkeypatch.setattr(IBKRFeed, "connect",
                        lambda self: (_ for _ in ()).throw(OSError("gateway down")))
    assert f.reconnect() is False          # never raises into the trading loop


def test_write_feed_state(tmp_path):
    p = tmp_path / "feed_state.json"
    write_feed_state(True, path=str(p))
    d = json.loads(p.read_text())
    assert d["connected"] is True and "ts" in d
    write_feed_state(False, path=str(p))
    assert json.loads(p.read_text())["connected"] is False


# --- underlying + tradingClass wiring -------------------------------------------------------
def test_underlying_index_vs_stock():
    f = IBKRFeed(symbol="XSP", underlying_sec_type="IND")
    u = f._underlying()
    assert type(u).__name__ == "Index" and u.exchange == "CBOE"
    g = IBKRFeed(symbol="SPY")             # default STK unchanged
    assert type(g._underlying()).__name__ == "Stock"


def test_trading_class_default_empty():
    f = IBKRFeed(symbol="SPY")
    assert f.trading_class == ""           # SPY path: no tradingClass constraint


# --- rehearsal overrides --------------------------------------------------------------------
def test_apply_rehearsal_overrides(cfg=None):
    cfg = load_config()
    base_symbol = cfg._data["symbol"]
    assert base_symbol == "SPY"            # base config must stay SPY (launchd session)
    apply_rehearsal(cfg)
    assert cfg._data["symbol"] == "XSP"
    ib = cfg._data["execution"]["ibkr"]
    assert ib["underlying_sec_type"] == "IND" and ib["trading_class"] == "XSP"
    assert cfg._data["memory"]["trade_log_path"] == "trades_rehearsal.db"
    assert REHEARSAL_RISK_STATE != "logs/risk_state.json"
