"""
src/monitoring/anomaly_detector.py — LLM latency anomaly detection.

Detects unusual spikes in LLM response time by comparing each call's
latency against a rolling baseline (p50 of the last N calls).

An anomaly is flagged when a single call exceeds the configured spike
factor times the rolling baseline.

Usage:
    from src.monitoring.anomaly_detector import LatencyAnomalyDetector

    detector = LatencyAnomalyDetector()
    detector.observe(2.5)   # normal
    detector.observe(15.0)  # anomaly if > 3x baseline
    stats = detector.get_stats()
"""

from __future__ import annotations

import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Rolling baseline window size.
BASELINE_WINDOW_SIZE: int = 100

# Default spike factor for anomaly detection.
DEFAULT_SPIKE_FACTOR: float = 3.0

# Window for counting recent anomalies (seconds).
ANOMALY_WINDOW_SECONDS: float = 3600.0  # 1 hour


@dataclass(slots=True)
class AnomalyStats:
    """Snapshot of anomaly detection state."""

    anomaly_count_last_hour: int = 0
    current_baseline_ms: float = 0.0
    last_anomaly_time: float | None = None
    last_anomaly_latency_ms: float = 0.0
    last_spike_factor: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "anomaly_count_last_hour": self.anomaly_count_last_hour,
            "current_baseline_ms": round(self.current_baseline_ms, 2),
            "last_anomaly_time": self.last_anomaly_time,
            "last_anomaly_latency_ms": round(self.last_anomaly_latency_ms, 2),
            "last_spike_factor": round(self.last_spike_factor, 2),
        }


class LatencyAnomalyDetector:
    """Detect latency anomalies against a rolling p50 baseline.

    Thread-safety: relies on asyncio's single-threaded event loop.
    """

    def __init__(
        self,
        enabled: bool = True,
        spike_factor: float = DEFAULT_SPIKE_FACTOR,
        window_size: int = BASELINE_WINDOW_SIZE,
    ) -> None:
        self._enabled = enabled
        self._spike_factor = spike_factor
        self._latencies: deque[float] = deque(maxlen=window_size)
        self._anomaly_timestamps: deque[float] = deque(maxlen=1000)
        self._last_anomaly_time: float | None = None
        self._last_anomaly_latency_ms: float = 0.0
        self._last_spike_factor: float = 0.0

    def observe(self, latency_seconds: float) -> bool:
        """Record a latency sample and return True if it's an anomaly."""
        latency_ms = latency_seconds * 1000
        self._latencies.append(latency_ms)

        if not self._enabled or len(self._latencies) < 10:
            return False

        baseline = statistics.median(self._latencies)
        if baseline <= 0:
            return False

        spike = latency_ms / baseline
        if spike > self._spike_factor:
            now = time.time()
            self._anomaly_timestamps.append(now)
            self._last_anomaly_time = now
            self._last_anomaly_latency_ms = latency_ms
            self._last_spike_factor = spike
            log.warning(
                "LLM latency anomaly detected | latency=%.0fms | "
                "baseline=%.0fms | spike=%.1fx",
                latency_ms,
                baseline,
                spike,
                extra={
                    "alert_type": "latency_anomaly",
                    "latency_ms": round(latency_ms, 2),
                    "baseline_ms": round(baseline, 2),
                    "spike_factor": round(spike, 2),
                },
            )
            return True

        return False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def spike_factor(self) -> float:
        return self._spike_factor

    @spike_factor.setter
    def spike_factor(self, value: float) -> None:
        self._spike_factor = value

    def get_stats(self) -> AnomalyStats:
        """Return current anomaly detection state."""
        now = time.time()
        cutoff = now - ANOMALY_WINDOW_SECONDS
        recent_count = sum(1 for ts in self._anomaly_timestamps if ts >= cutoff)

        baseline = statistics.median(self._latencies) if len(self._latencies) >= 2 else 0.0

        return AnomalyStats(
            anomaly_count_last_hour=recent_count,
            current_baseline_ms=baseline,
            last_anomaly_time=self._last_anomaly_time,
            last_anomaly_latency_ms=self._last_anomaly_latency_ms,
            last_spike_factor=self._last_spike_factor,
        )
