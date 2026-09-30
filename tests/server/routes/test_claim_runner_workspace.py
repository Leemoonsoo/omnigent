"""The host claim route validates ownership and updates the runner and store."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server.routes import _workspace_validation
from omnigent.server.routes import hosts as hosts_routes


class _Store:
    def __init__(self, conv: SimpleNamespace) -> None:
        self.conv = conv
        self.writes: list[str] = []

    def get_conversation(self, session_id: str) -> SimpleNamespace:
        assert session_id == self.conv.id
        return self.conv

    def claim_runner_workspace(
        self,
        session_id: str,
        *,
        host_id: str,
        runner_id: str,
        expected_workspace: str,
        workspace: str,
    ) -> bool:
        assert session_id == self.conv.id
        assert host_id == self.conv.host_id
        assert runner_id == self.conv.runner_id
        if self.conv.workspace != expected_workspace:
            return False
        self.conv.workspace = workspace
        self.writes.append(workspace)
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("runner_status", [200, 409])
async def test_claim_persists_only_after_runner_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_status: int,
) -> None:
    old_workspace = str(tmp_path / "staging")
    workspace = str(tmp_path / "project")
    conv = SimpleNamespace(
        id="conv_test", host_id="host_test", runner_id="runner_test",
        workspace=old_workspace, git_branch=None,
    )
    store = _Store(conv)
    calls: list[dict[str, Any]] = []

    class _RunnerClient:
        async def post(self, path: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
            assert path == "/v1/runner/claim-workspace"
            assert timeout > 0
            calls.append(json)
            return httpx.Response(
                runner_status,
                json={"workspace": workspace} if runner_status == 200 else {"detail": "rejected"},
            )

    class _Router:
        def client_for_session_resources(
            self, session_id: str, *, conversation: SimpleNamespace
        ) -> SimpleNamespace:
            assert session_id == conv.id and conversation is conv
            return SimpleNamespace(client=_RunnerClient())

    monkeypatch.setattr(
        hosts_routes,
        "resolve_host_launch",
        lambda **kwargs: SimpleNamespace(
            host=SimpleNamespace(name="test host"), conn=None, conv=conv
        ),
    )

    async def _spec_cwd(*args: Any) -> None:
        return None

    async def _validate(**kwargs: Any) -> str:
        assert kwargs["workspace"] == workspace
        return workspace

    monkeypatch.setattr(hosts_routes, "_resolve_agent_spec_cwd", _spec_cwd)
    monkeypatch.setattr(_workspace_validation, "validate_workspace", _validate)
    app = FastAPI()
    app.include_router(
        hosts_routes.create_hosts_router(
            host_registry=object(),  # type: ignore[arg-type]
            host_store=object(),  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
            agent_store=object(),  # type: ignore[arg-type]
            agent_cache=object(),  # type: ignore[arg-type]
            runner_router=_Router(),  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/hosts/host_test/runners/runner_test/claim-workspace",
            json={"session_id": conv.id, "workspace": workspace},
        )

    assert calls == [{
        "session_id": conv.id,
        "workspace": workspace,
        "expected_workspace": old_workspace,
    }]
    if runner_status == 200:
        assert response.status_code == 200
        assert store.writes == [workspace]
        assert conv.workspace == workspace
    else:
        assert response.status_code == 409
        assert store.writes == []
        assert conv.workspace == old_workspace


@pytest.mark.asyncio
async def test_lost_claim_response_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_workspace = str(tmp_path / "staging")
    workspace = str(tmp_path / "project")
    conv = SimpleNamespace(
        id="conv_test", host_id="host_test", runner_id="runner_test",
        workspace=old_workspace, git_branch=None,
    )
    store = _Store(conv)
    claims = 0

    class _RunnerClient:
        async def post(self, path: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
            nonlocal claims
            assert path == "/v1/runner/claim-workspace"
            assert json["expected_workspace"] == old_workspace
            claims += 1
            if claims == 1:
                raise httpx.ReadError("response lost")
            return httpx.Response(200, json={"workspace": workspace})

    class _Router:
        def client_for_session_resources(
            self, session_id: str, *, conversation: SimpleNamespace
        ) -> SimpleNamespace:
            assert session_id == conv.id and conversation is conv
            return SimpleNamespace(client=_RunnerClient())

    monkeypatch.setattr(
        hosts_routes,
        "resolve_host_launch",
        lambda **kwargs: SimpleNamespace(
            host=SimpleNamespace(name="test host"), conn=None, conv=conv
        ),
    )

    async def _spec_cwd(*args: Any) -> None:
        return None

    async def _validate(**kwargs: Any) -> str:
        return workspace

    monkeypatch.setattr(hosts_routes, "_resolve_agent_spec_cwd", _spec_cwd)
    monkeypatch.setattr(_workspace_validation, "validate_workspace", _validate)
    app = FastAPI()
    app.include_router(
        hosts_routes.create_hosts_router(
            host_registry=object(),  # type: ignore[arg-type]
            host_store=object(),  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
            agent_store=object(),  # type: ignore[arg-type]
            agent_cache=object(),  # type: ignore[arg-type]
            runner_router=_Router(),  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/v1/hosts/host_test/runners/runner_test/claim-workspace",
            json={"session_id": conv.id, "workspace": workspace},
        )
        assert first.status_code == 504
        assert store.writes == []
        second = await client.post(
            "/v1/hosts/host_test/runners/runner_test/claim-workspace",
            json={"session_id": conv.id, "workspace": workspace},
        )
    assert second.status_code == 200
    assert claims == 2
    assert store.writes == [workspace]
