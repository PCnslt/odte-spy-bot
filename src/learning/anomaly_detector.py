"""Anomaly detection: price shocks, IV spikes, execution latency, and stale data.

Any anomaly returns an action the main loop must honor. Fail-closed: when in doubt, HALT.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import numpy as np


class AnomalyAction(str, Enum):
    NONE = "none"
    REDUCE_RISK = "reduce_risk"   # widen stops / shrink size
    HALT = "halt"                 # flatten + pause new entries


@dataclass
class AnomalyResult:
    kinds: list[str]
    action: AnomalyAction


class AnomalyDetector:
    def __init__(self, cfg):
        a = cfg.risk["anomaly"]
        self.price_sigma = a["price_sigma"]
        self.iv_spike_mult = a["iv_spike_mult"]
        self.max_latency_ms = a["max_exec_latency_ms"]
        self.max_staleness_s = a["max_data_staleness_s"]
        self._returns = deque(maxlen=200)
        self._ivs = deque(maxlen=200)

    def observe(self, ret_1: float, iv: float) -> None:
        self._returns.append(ret_1)
        self._ivs.append(iv)

    def check(self, ret_1: float, iv: float, data_age_s: float = 0.0,
              last_latency_ms: float = 0.0) -> AnomalyResult:
        kinds: list[str] = []
        action = AnomalyAction.NONE

        if len(self._returns) >= 30:
            std = float(np.std(self._returns)) or 1e-9
            if abs(ret_1) > self.price_sigma * std:
                kinds.append("PRICE_SHOCK")
                action = AnomalyAction.HALT

        if len(self._ivs) >= 30:
            mean_iv = float(np.mean(self._ivs)) or 1e-9
            if iv > self.iv_spike_mult * mean_iv:
                kinds.append("IV_SPIKE")
                action = max(action, AnomalyAction.REDUCE_RISK, key=_severity)

        if data_age_s > self.max_staleness_s:
            kinds.append("STALE_DATA")
            action = AnomalyAction.HALT

        if last_latency_ms > self.max_latency_ms:
            kinds.append("EXEC_LATENCY")
            action = max(action, AnomalyAction.REDUCE_RISK, key=_severity)

        return AnomalyResult(kinds, action)


def _severity(a: AnomalyAction) -> int:
    return {AnomalyAction.NONE: 0, AnomalyAction.REDUCE_RISK: 1, AnomalyAction.HALT: 2}[a]
