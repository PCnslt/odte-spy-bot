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

    def validate(self) -> list[str]:
        """Lint the calendar (audit m5): parse errors, invalid actions, stale dates."""
        problems: list[str] = []
        if self.path.exists() and not self._events:
            try:
                raw = yaml.safe_load(self.path.read_text()) or {}
                if raw.get("events"):
                    problems.append("events present but none parsed — check date/format")
            except Exception as exc:
                problems.append(f"YAML parse error: {exc}")
        today = date.today()
        for d, ev in sorted(self._events.items()):
            if ev["action"] not in ("block", "widen"):
                problems.append(f"{d} {ev['name']}: invalid action '{ev['action']}'")
            if d < today:
                problems.append(f"{d} {ev['name']}: date is in the past (stale entry)")
        return problems


def _main() -> None:  # python -m src.utils.events --validate
    import argparse

    p = argparse.ArgumentParser(description="Event-calendar lint")
    p.add_argument("--path", default="config/events.yaml")
    p.add_argument("--validate", action="store_true")
    args = p.parse_args()
    g = EventGuard(args.path)
    problems = g.validate()
    if problems:
        for x in problems:
            print(f"PROBLEM: {x}")
        raise SystemExit(1)
    print(f"events.yaml OK ({len(g._events)} events)")


if __name__ == "__main__":
    _main()
