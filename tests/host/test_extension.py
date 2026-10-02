"""Optional host extension loading, lifecycle, and child ownership."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from omnigent.host.connect import HostProcess
from omnigent.host.extension import HostExtension, load_host_extension
from omnigent.host.identity import HostIdentity


class ExampleExtension(HostExtension):
    def __init__(self, *, fail_start: bool = False) -> None:
        self.pids: set[int] = set()
        self.fail_start = fail_start
        self.started = False
        self.stopped = False

    @property
    def owned_pids(self) -> set[int]:
        return set(self.pids)

    async def start(self) -> None:
        self.started = True
        if self.fail_start:
            raise RuntimeError("synthetic startup failure")

    async def stop(self) -> None:
        self.stopped = True


def _host(extension: HostExtension) -> HostProcess:
    return HostProcess(
        identity=HostIdentity(host_id="host_extension_test", name="extension-test"),
        server_url="http://localhost:8000",
        interactive_shells=["bash"],
        host_extension=extension,
    )


def test_host_extension_requires_explicit_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_HOST_EXTENSION", raising=False)
    monkeypatch.setattr(
        "omnigent.host.extension.importlib.metadata.entry_points",
        lambda **_kwargs: pytest.fail("entry points should not be read without opt-in"),
    )
    assert load_host_extension() is None


def test_selected_installed_host_extension_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_EXTENSION", "example")
    monkeypatch.setattr(
        "omnigent.host.extension.importlib.metadata.entry_points",
        lambda **_kwargs: [SimpleNamespace(name="example", load=lambda: ExampleExtension)],
    )
    assert isinstance(load_host_extension(), ExampleExtension)


@pytest.mark.asyncio
async def test_failed_extension_start_is_cleaned_up_without_blocking_host() -> None:
    extension = ExampleExtension(fail_start=True)
    host = _host(extension)

    await host._start_host_extension()

    assert extension.started
    assert extension.stopped
    assert host._host_extension is None


@pytest.mark.posix_only
def test_extension_child_retains_its_exit_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_ZYGOTE", "0")
    extension = ExampleExtension()
    host = _host(extension)
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(42)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.read() == b""
        extension.pids.add(process.pid)
        assert process.pid in host._tracked_runner_pids()
        assert host._reap_orphans_once([process.pid]) == 0
        assert process.wait(timeout=5) == 42
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
