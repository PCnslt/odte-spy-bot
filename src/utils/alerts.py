"""Alerts. Telegram if configured, otherwise falls back to logging (never crashes the bot)."""
from __future__ import annotations

import requests

from .logger import get_logger

log = get_logger("alerts")


class Alerter:
    def __init__(self, enabled: bool, bot_token: str = "", chat_id: str = ""):
        self.enabled = bool(enabled and bot_token and chat_id)
        self.bot_token = bot_token
        self.chat_id = chat_id
        if enabled and not self.enabled:
            log.warning("Telegram alerts requested but token/chat_id missing; logging only.")

    def send(self, message: str, level: str = "INFO") -> None:
        line = f"[{level}] {message}"
        log.info("alert: %s", line)
        if not self.enabled:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": line},
                timeout=10,
            )
        except Exception as exc:  # never let alerting break trading
            log.warning("Telegram send failed: %s", exc)

    @classmethod
    def from_config(cls, cfg) -> "Alerter":
        return cls(
            enabled=cfg.alerts.get("telegram_enabled", False),
            bot_token=cfg.secrets.get("telegram_bot_token", ""),
            chat_id=cfg.secrets.get("telegram_chat_id", ""),
        )
