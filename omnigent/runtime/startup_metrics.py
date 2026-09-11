"""OpenTelemetry metrics for foreground CLI startup phases."""

from __future__ import annotations

import atexit
import logging
from functools import lru_cache
from typing import Protocol

from omnigent.runtime.telemetry import telemetry_enabled

_logger = logging.getLogger(__name__)

STARTUP_PHASE_DURATION_METRIC_NAME = "omnigent.client.startup.phase.duration"
_OTEL_METER_NAME = "omnigent.client.startup"
_EXIT_FLUSH_TIMEOUT_MS = 3_000
_flush_registered = False


class _Histogram(Protocol):
    def record(self, amount: int | float, attributes: dict[str, str] | None = None) -> None: ...


@lru_cache(maxsize=1)
def _duration_histogram() -> _Histogram:
    from opentelemetry import metrics as otel_metrics

    return otel_metrics.get_meter(_OTEL_METER_NAME).create_histogram(
        STARTUP_PHASE_DURATION_METRIC_NAME,
        unit="ms",
        description="Foreground CLI startup phase duration.",
    )


def _flush_startup_metrics() -> None:
    """Best-effort flush for CLIs that exit before the periodic export."""
    try:
        from opentelemetry import metrics as otel_metrics

        provider = otel_metrics.get_meter_provider()
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is not None:
            force_flush(timeout_millis=_EXIT_FLUSH_TIMEOUT_MS)
    except Exception:
        _logger.debug("failed to flush startup metrics", exc_info=True)


def _register_exit_flush() -> None:
    global _flush_registered

    if _flush_registered:
        return
    _flush_registered = True
    atexit.register(_flush_startup_metrics)


def record_startup_phase_duration(
    duration_ms: float,
    *,
    phase: str,
    harness: str,
    outcome: str,
) -> None:
    """Record one bounded foreground startup phase, best effort."""
    if not telemetry_enabled():
        return
    try:
        _duration_histogram().record(
            max(0.0, duration_ms),
            attributes={
                "phase": phase,
                "harness": harness,
                "outcome": outcome,
            },
        )
        _register_exit_flush()
    except Exception:
        _logger.debug("failed to record startup phase metric", exc_info=True)
