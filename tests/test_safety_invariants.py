"""Invariants that must hold no matter what the broker or the process does.

1. A daily-loss halt survives a crash + relaunch (it used to reset, re-arming a fresh budget).
2. An unconfirmed close records NO P&L (it used to book the entry credit as the exit cost,
   turning a ~-$400 loss into -$5.20).
3. reconcile never zeroes a dangling row while the broker still holds positions.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime

import pytest

from src.execution.ibkr_broker import IBKRBroker
from src.execution.position_manager import PositionManager
from src.reconcile import BrokerSnap, reconcile
from src.utils.trade_log import TradeLog


# --- 0. the port is only a convention: never trade a LIVE account in paper mode --------------
class _AcctIB:
    def __init__(self, accounts):
        self._accounts = accounts
        self.disconnected = False

    def managedAccounts(self):
        return self._accounts

    def disconnect(self):
        self.disconnected = True


def test_paper_mode_refuses_a_live_account(cfg):
    """If the Gateway on the paper port is logged into the LIVE account, we must refuse —
    otherwise `--mode paper` sends real-money 0DTE orders."""
    b = IBKRBroker(cfg, mode="paper")
    b.ib = _AcctIB(["U1234567"])          # live accounts do not start with 'D'
    with pytest.raises(RuntimeError, match="REFUSING TO TRADE"):
        b._assert_account_matches_mode()
    assert b.ib.disconnected is True


def test_paper_mode_accepts_a_paper_account(cfg):
    b = IBKRBroker(cfg, mode="paper")
    b.ib = _AcctIB(["DUR193467"])
    b._assert_account_matches_mode()      # must not raise


def test_refuses_when_no_managed_accounts(cfg):
    b = IBKRBroker(cfg, mode="paper")
    b.ib = _AcctIB([])
    with pytest.raises(RuntimeError, match="no managed accounts"):
        b._assert_account_matches_mode()


# --- 1. durable risk state ------------------------------------------------------------------
def test_daily_loss_halt_survives_restart(cfg, tmp_path):
    sp = tmp_path / "risk_state.json"
    pm = PositionManager(cfg, state_path=sp)
    now = datetime.now()
    pm._roll_day(now)
    # blow through the daily loss limit
    pm.record_result(-1_000_000.0)
    ok, why = pm.can_open(now, equity=100_000.0, open_count=0)
    assert not ok and why == "daily_loss_halt" and pm.halted

    # crash + relaunch: a brand-new PositionManager on the same state file
    pm2 = PositionManager(cfg, state_path=sp)
    assert pm2.halted is True
    ok2, why2 = pm2.can_open(now, equity=100_000.0, open_count=0)
    assert not ok2 and why2 == "halted"


def test_trade_count_survives_restart(cfg, tmp_path):
    sp = tmp_path / "risk_state.json"
    pm = PositionManager(cfg, state_path=sp)
    pm._roll_day(datetime.now())
    for _ in range(pm.max_trades_per_day):
        pm.on_open()
    pm2 = PositionManager(cfg, state_path=sp)
    ok, why = pm2.can_open(datetime.now(), equity=100_000.0, open_count=0)
    assert not ok and why == "max_trades_per_day"


def test_no_persistence_without_state_path(cfg, tmp_path, monkeypatch):
    """Backtests/tests must not write risk state into the repo."""
    monkeypatch.chdir(tmp_path)
    pm = PositionManager(cfg)          # no state_path
    pm.on_open()
    pm.record_result(-5.0)
    assert not (tmp_path / "logs").exists()


# --- 2. unconfirmed close writes NO pnl ------------------------------------------------------
def test_mark_unconfirmed_leaves_pnl_null(tmp_path):
    db = tmp_path / "t.db"
    tl = TradeLog(db)
    tid = tl.open_trade(opened_at="2026-07-09T10:36:00", kind="bear_call", quantity=2,
                        credit_est=1.42)
    tl.mark_unconfirmed(tid, closed_at="2026-07-09T16:00:00")
    tl.close()

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert r["closed_at"] == "2026-07-09T16:00:00"
    assert r["exit_reason"] == "unconfirmed_eod"
    assert r["pnl"] is None            # never a fabricated number


# --- 3. reconcile won't zero a row while the broker holds positions --------------------------
def _seed_dangling(db):
    tl = TradeLog(db)
    tl.open_trade(opened_at="2026-07-09T10:00:00", kind="bull_put", quantity=5, credit_est=1.0)
    tl.close()


def test_resolve_refuses_when_broker_not_flat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "t.db")
    _seed_dangling(db)
    # broker reachable but STILL HOLDING a leg -> a dangling row may be a real open position
    monkeypatch.setattr("src.reconcile.broker_snapshot",
                        lambda *a, **k: BrokerSnap(True, net_liq=1_000_000.0,
                                                   orphans=[{"localSymbol": "SPY", "right": "C",
                                                             "strike": 1.0, "position": -2,
                                                             "avgCost": 1.0}]))
    res = reconcile(date(2026, 7, 9), db_path=db, ledger_path=str(tmp_path / "l.jsonl"),
                    resolve=True)
    assert res["resolved"] == []
    assert "RESOLVE SKIPPED" in res["report"]


def test_resolve_refuses_when_broker_unavailable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "t.db")
    _seed_dangling(db)
    monkeypatch.setattr("src.reconcile.broker_snapshot",
                        lambda *a, **k: BrokerSnap(False, note="gateway down"))
    res = reconcile(date(2026, 7, 9), db_path=db, ledger_path=str(tmp_path / "l.jsonl"),
                    resolve=True)
    assert res["resolved"] == [] and "RESOLVE SKIPPED" in res["report"]


def test_resolve_proceeds_when_broker_confirmed_flat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "t.db")
    _seed_dangling(db)
    monkeypatch.setattr("src.reconcile.broker_snapshot",
                        lambda *a, **k: BrokerSnap(True, net_liq=1_000_000.0, orphans=[]))
    res = reconcile(date(2026, 7, 9), db_path=db, ledger_path=str(tmp_path / "l.jsonl"),
                    resolve=True)
    assert len(res["resolved"]) == 1
