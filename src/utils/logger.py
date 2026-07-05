"""Structured logging to console + rotating file. JSON lines on disk, readable on console."""
from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Attach any structured extras.
        for key, value in record.__dict__.items():
            if key.startswith("x_"):
                payload[key[2:]] = value
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO", log_dir: str | Path = "logs") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
    root.addHandler(console)

    fileh = RotatingFileHandler(log_dir / "bot.jsonl", maxBytes=5_000_000, backupCount=5)
    fileh.setFormatter(_JsonFormatter())
    root.addHandler(fileh)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)
