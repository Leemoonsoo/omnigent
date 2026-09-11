"""Tests for foreground startup phase metrics."""

from __future__ import annotations

from opentelemetry import metrics as otel_metrics

from omnigent.runtime import startup_metrics


class _FakeHistogram:
    def __init__(self) -> None:
        self.records: list[tuple[float, dict[str, str]]] = []

    def record(self, amount: float, attributes: dict[str, str] | None = None) -> None:
        self.records.append((amount, attributes or {}))


def test_record_startup_phase_duration_records_bounded_attributes(monkeypatch) -> None:
    histogram = _FakeHistogram()
    registered: list[bool] = []
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    monkeypatch.setattr(startup_metrics, "_duration_histogram", lambda: histogram)
    monkeypatch.setattr(startup_metrics, "_register_exit_flush", lambda: registered.append(True))

    startup_metrics.record_startup_phase_duration(
        123.5,
        phase="wait_runner",
        harness="codex",
        outcome="success",
    )

    assert histogram.records == [
        (
            123.5,
            {"phase": "wait_runner", "harness": "codex", "outcome": "success"},
        )
    ]
    assert registered == [True]


def test_record_startup_phase_duration_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("OMNIGENT_TELEMETRY_ENABLED", raising=False)
    monkeypatch.setattr(
        startup_metrics,
        "_duration_histogram",
        lambda: (_ for _ in ()).throw(AssertionError("histogram should not be created")),
    )

    startup_metrics.record_startup_phase_duration(
        10,
        phase="connect_server",
        harness="codex",
        outcome="success",
    )


def test_exit_flush_uses_bounded_timeout(monkeypatch) -> None:
    calls: list[int] = []

    class _Provider:
        def force_flush(self, *, timeout_millis: int) -> None:
            calls.append(timeout_millis)

    monkeypatch.setattr(otel_metrics, "get_meter_provider", lambda: _Provider())

    startup_metrics._flush_startup_metrics()

    assert calls == [3000]


def test_exit_flush_is_registered_once(monkeypatch) -> None:
    callbacks: list[object] = []
    monkeypatch.setattr(startup_metrics, "_flush_registered", False)
    monkeypatch.setattr(startup_metrics.atexit, "register", callbacks.append)

    startup_metrics._register_exit_flush()
    startup_metrics._register_exit_flush()

    assert callbacks == [startup_metrics._flush_startup_metrics]
