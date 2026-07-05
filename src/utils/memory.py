"""Persistent trading memory (SQLite). Prevents whipsaw and contradictory rapid-fire signals.

Replaces the Redis design from the spec with a zero-dependency embedded DB. Stores the current
directional bias per symbol and a rolling decision log, and enforces two consistency rules:

  1. Time gate     - no new decision within `time_gate_minutes` of the last one.
  2. Whipsaw guard - no more than `max_bias_changes_per_hour` bias flips in a rolling hour.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .logger import get_logger

log = get_logger("memory")


class TradingMemory:
    def __init__(self, db_path: str | Path = "memory.db",
                 time_gate_minutes: int = 3, max_bias_changes_per_hour: int = 2):
        self.db_path = str(db_path)
        self.time_gate = timedelta(minutes=time_gate_minutes)
        self.max_bias_changes = max_bias_changes_per_hour
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                ts TEXT NOT NULL,
                bias TEXT NOT NULL,
                confidence REAL,
                reasoning TEXT,
                invalidation_level REAL
            );
            CREATE TABLE IF NOT EXISTS bias (
                symbol TEXT PRIMARY KEY,
                bias TEXT,
                confidence REAL,
                reasoning TEXT,
                invalidation_level REAL,
                established_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_dec_symbol_ts ON decisions(symbol, ts);
            """
        )
        self._conn.commit()

    # --- reads -----------------------------------------------------------------
    def get_current_bias(self, symbol: str) -> dict:
        row = self._conn.execute("SELECT * FROM bias WHERE symbol = ?", (symbol,)).fetchone()
        if not row:
            return {"bias": None, "message": f"No bias established for {symbol}"}
        established = datetime.fromisoformat(row["established_at"])
        return {
            "bias": row["bias"],
            "confidence": row["confidence"],
            "reasoning": row["reasoning"],
            "invalidation_level": row["invalidation_level"],
            "established_at": row["established_at"],
            "time_held_minutes": (datetime.now() - established).total_seconds() / 60.0,
        }

    def _recent_decisions(self, symbol: str, since: datetime) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM decisions WHERE symbol = ? AND ts >= ? ORDER BY ts",
            (symbol, since.isoformat()),
        ).fetchall()

    # --- consistency gate ------------------------------------------------------
    def check_consistency(self, symbol: str, proposed_bias: str,
                          now: Optional[datetime] = None) -> tuple[bool, str]:
        """Return (allowed, reason). `proposed_bias` in {'bullish','bearish'}."""
        now = now or datetime.now()

        last = self._conn.execute(
            "SELECT * FROM decisions WHERE symbol = ? ORDER BY ts DESC LIMIT 1", (symbol,)
        ).fetchone()

        if last is not None:
            last_ts = datetime.fromisoformat(last["ts"])
            if now - last_ts < self.time_gate:
                return False, "time_gate"

        # Whipsaw: count bias flips in the trailing hour.
        window = self._recent_decisions(symbol, now - timedelta(hours=1))
        flips = 0
        prev = None
        for d in window:
            if prev is not None and d["bias"] != prev:
                flips += 1
            prev = d["bias"]
        if prev is not None and proposed_bias != prev:
            flips += 1
        if flips > self.max_bias_changes:
            return False, "whipsaw_guard"

        return True, "ok"

    # --- writes ----------------------------------------------------------------
    def store_decision(self, symbol: str, bias: str, confidence: float = 0.0,
                       reasoning: str = "", invalidation_level: float = 0.0,
                       now: Optional[datetime] = None) -> None:
        now = now or datetime.now()
        self._conn.execute(
            "INSERT INTO decisions(symbol, ts, bias, confidence, reasoning, invalidation_level)"
            " VALUES (?,?,?,?,?,?)",
            (symbol, now.isoformat(), bias, confidence, reasoning, invalidation_level),
        )
        self._conn.execute(
            "INSERT INTO bias(symbol, bias, confidence, reasoning, invalidation_level,"
            " established_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(symbol) DO UPDATE SET bias=excluded.bias,"
            " confidence=excluded.confidence, reasoning=excluded.reasoning,"
            " invalidation_level=excluded.invalidation_level,"
            " established_at=excluded.established_at",
            (symbol, bias, confidence, reasoning, invalidation_level, now.isoformat()),
        )
        self._conn.commit()

    def prune(self, older_than_days: int = 7, now: Optional[datetime] = None) -> None:
        now = now or datetime.now()
        cutoff = (now - timedelta(days=older_than_days)).isoformat()
        self._conn.execute("DELETE FROM decisions WHERE ts < ?", (cutoff,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @classmethod
    def from_config(cls, cfg) -> "TradingMemory":
        return cls(
            db_path=cfg.memory.get("db_path", "memory.db"),
            time_gate_minutes=cfg.memory.get("time_gate_minutes", 3),
            max_bias_changes_per_hour=cfg.memory.get("max_bias_changes_per_hour", 2),
        )
