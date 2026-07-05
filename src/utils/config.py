"""Configuration loading: merges the three YAMLs + .env into one dotted-access object."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv is a hard dep but stay defensive
    def load_dotenv(*_a, **_k):  # type: ignore
        return False

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


class Config:
    """Dotted, dict-like access over merged config. `cfg.execution.mode`, `cfg["symbol"]`."""

    def __init__(self, data: dict[str, Any]):
        self._data = data
        for key, value in data.items():
            setattr(self, key, Config(value) if isinstance(value, dict) else value)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def to_dict(self) -> dict[str, Any]:
        return self._data

    def __repr__(self) -> str:  # pragma: no cover
        return f"Config({self._data!r})"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def load_config(config_dir: Path | str = CONFIG_DIR) -> Config:
    """Load config.yaml + risk_params.yaml + model_params.yaml and .env secrets."""
    load_dotenv(ROOT / ".env")
    config_dir = Path(config_dir)

    merged = _load_yaml(config_dir / "config.yaml")
    merged["risk"] = _load_yaml(config_dir / "risk_params.yaml")
    merged["model_params"] = _load_yaml(config_dir / "model_params.yaml")

    # Selected secrets surfaced onto the config for convenience.
    merged["secrets"] = {
        "polygon_api_key": os.getenv("POLYGON_API_KEY", ""),
        "ibkr_account": os.getenv("IBKR_ACCOUNT", ""),
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
        "hf_token": os.getenv("HF_TOKEN", ""),
    }
    merged["_root"] = str(ROOT)
    return Config(merged)
