"""Optional financial-news sentiment via Hugging Face FinBERT.

Disabled by default: the whole project runs offline without transformers/torch. When enabled,
the model is lazy-loaded on first use. `analyze()` returns a score in [-1, +1]; if the model
can't load, it degrades to 0.0 (neutral) rather than crashing the bot.
"""
from __future__ import annotations

from typing import Optional

from ..utils.logger import get_logger

log = get_logger("sentiment")


class SentimentAnalyzer:
    def __init__(self, enabled: bool = False, model: str = "ProsusAI/finbert",
                 hf_token: str = ""):
        self.enabled = enabled
        self.model_name = model
        self.hf_token = hf_token or None
        self._pipeline = None
        self._failed = False

    def _ensure_pipeline(self) -> bool:
        if self._pipeline is not None:
            return True
        if self._failed or not self.enabled:
            return False
        try:
            from transformers import pipeline
            self._pipeline = pipeline(
                "sentiment-analysis", model=self.model_name, device=-1, token=self.hf_token,
            )
            log.info("Loaded sentiment model %s", self.model_name)
            return True
        except Exception as exc:  # missing deps, no network, etc.
            log.warning("Sentiment disabled (load failed): %s", exc)
            self._failed = True
            return False

    def analyze(self, text: str) -> float:
        if not text or not self._ensure_pipeline():
            return 0.0
        try:
            result = self._pipeline(text[:512])[0]
        except Exception as exc:
            log.warning("Sentiment inference failed: %s", exc)
            return 0.0
        label = result["label"].lower()
        score = float(result["score"])
        if "pos" in label:
            return score
        if "neg" in label:
            return -score
        return 0.0

    def analyze_headlines(self, headlines: list[str]) -> float:
        if not headlines:
            return 0.0
        scores = [self.analyze(h) for h in headlines]
        return sum(scores) / len(scores) if scores else 0.0

    @classmethod
    def from_config(cls, cfg) -> "SentimentAnalyzer":
        return cls(
            enabled=cfg.sentiment.get("enabled", False),
            model=cfg.sentiment.get("model", "ProsusAI/finbert"),
            hf_token=cfg.secrets.get("hf_token", ""),
        )
