"""Economic-event guard. Reads a local YAML calendar (reliable; no scraping in the trading
loop) and tells the loop whether today is an event day and what to do about it.

config/events.yaml format:
    events:
      - date: 2026-07-15
        name: CPI
        action: block          # block = no new entries that day
      - date: 2026-07-29
        name: FOMC
        action: widen          # widen = require extra range margin (safety multiplier x2)

Empty/missing file => no-op (trades normally). Maintain the dates yourself — FOMC/CPI
calendars are published a year ahead. Fail-safe: a malformed file logs a warning and
disables the guard rather than crashing the session.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from .logger import get_logger

log = get_logger("events")


class EventGuard:
    def __init__(self, path: str | Path = "config/events.yaml"):
        self.path = Path(path)
        self._events: dict[date, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = yaml.safe_load(self.path.read_text()) or {}
            for ev in data.get("events", []) or []:
                d = ev.get("date")
                if isinstance(d, str):
                    d = date.fromisoformat(d)
                if isinstance(d, date):
                    self._events[d] = {"name": ev.get("name", "event"),
                                       "action": ev.get("action", "block")}
            if self._events:
                log.info("Event calendar loaded: %d events", len(self._events))
        except Exception as exc:
            log.warning("events.yaml malformed (%s); event guard disabled.", exc)
            self._events = {}

    def check(self, d: date) -> Optional[dict]:
        """Return {'name','action'} if `d` is an event day, else None."""
        return self._events.get(d)
