"""Holdout-integrity guard — makes the reserved holdout physically un-loadable by accident.

The whole "profitability must be PROVABLE on an untouched holdout" claim (RESEARCH_PROTOCOL.md
§1) rests on the 2025-01-02 → 2025-06-30 window never being seen during development. Until now
that was enforced by discipline alone. This guard enforces it in code, fail-closed:

  * Any data-loader range that INTERSECTS the holdout raises, UNLESS the caller has explicitly
    authorized a pre-registered confirmatory look via `ODTE_CONFIRM=<HID>` (e.g. H5).
  * A look is CONSUMABLE ONCE per hypothesis: the guard records each spent HID in a ledger
    (models/holdout_ledger.json). Re-using a HID raises — you cannot peek twice and call it one
    look. This is the anti-HARKing budget made mechanical.

Idiom matches the existing `ODTE_TZ_OVERRIDE` env override in src/main.py — a loud, deliberate
escape hatch, never a silent default.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from .logger import get_logger

log = get_logger("holdout")

# The reserved, never-touched confirmatory holdout (RESEARCH_PROTOCOL.md §1).
HOLDOUT_START = date(2025, 1, 2)
HOLDOUT_END = date(2025, 6, 30)
DEFAULT_LEDGER = Path("models/holdout_ledger.json")


class HoldoutViolation(RuntimeError):
    """Raised when a data range would touch the reserved holdout without a valid, unused look."""


def intersects_holdout(start: date, end: date) -> bool:
    """True iff [start, end] overlaps [HOLDOUT_START, HOLDOUT_END] at all."""
    return not (end < HOLDOUT_START or start > HOLDOUT_END)


def _read_ledger(path: Path) -> list[str]:
    try:
        return list(json.loads(Path(path).read_text()))
    except Exception:
        return []


def _write_ledger(path: Path, ids: list[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(sorted(set(ids)), indent=2))


def guard(start: date, end: date, *, ledger_path: Path | str = DEFAULT_LEDGER,
          env: dict | None = None) -> None:
    """Fail-closed holdout check. No-op unless [start, end] touches the holdout.

    On intersection:
      - `ODTE_CONFIRM=<HID>` set and HID not yet spent -> allow ONCE, record HID (loud warn).
      - HID already spent, or no token -> raise HoldoutViolation.
    """
    if not intersects_holdout(start, end):
        return
    env = os.environ if env is None else env
    token = (env.get("ODTE_CONFIRM") or "").strip()
    if not token:
        raise HoldoutViolation(
            f"Range {start}..{end} intersects the RESERVED holdout "
            f"({HOLDOUT_START}..{HOLDOUT_END}). Refusing to load it. This window is for "
            "pre-registered confirmatory looks ONLY. If this is one, set ODTE_CONFIRM=<Hxx> "
            "for the pre-registered hypothesis; otherwise you are about to contaminate the "
            "one dataset that can still prove this strategy.")
    spent = _read_ledger(ledger_path)
    if token in spent:
        raise HoldoutViolation(
            f"Confirmatory look '{token}' was ALREADY consumed (ledger: {ledger_path}). "
            "Each hypothesis gets exactly one holdout look — no re-runs with tweaks. "
            "Register a new hypothesis ID to look again.")
    spent.append(token)
    _write_ledger(ledger_path, spent)
    log.warning("HOLDOUT LOOK CONSUMED: hypothesis '%s' authorized to read %s..%s. This is a "
                "ONE-TIME, pre-registered confirmatory test — results are final.",
                token, start, end)


def ledger_status(ledger_path: Path | str = DEFAULT_LEDGER) -> dict:
    """For the dashboard/experiment cockpit: which confirmatory looks have been spent."""
    spent = _read_ledger(ledger_path)
    return {"holdout": f"{HOLDOUT_START}..{HOLDOUT_END}", "consumed_looks": spent,
            "n_consumed": len(spent)}
