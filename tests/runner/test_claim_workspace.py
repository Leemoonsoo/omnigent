"""Prepared runner workspace claims must precede all session initialization."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.debug_logging import PRIMARY_SESSION_ID_ENV_VAR
from omnigent.runner import create_runner_app
from omnigent.runner.identity import RUNNER_WORKSPACE_ENV_VAR
from tests.runner.conftest import _runner_client
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
async def test_prepared_runner_claims_workspace_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    target = tmp_path / "project"
    staging.mkdir()
    target.mkdir()
    session_id = "conv_prepared"
    monkeypatch.chdir(staging)
    monkeypatch.setenv(PRIMARY_SESSION_ID_ENV_VAR, session_id)
    monkeypatch.setenv(RUNNER_WORKSPACE_ENV_VAR, str(staging))
    app = create_runner_app(
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=staging,
        per_session_workspace=False,
        allow_workspace_claim=True,
    )
    try:
        async with _runner_client(app) as client:
            unclaimed = await client.post(
                "/v1/sessions", json={"session_id": session_id, "agent_id": "agent_test"}
            )
            assert unclaimed.status_code == 409
            assert unclaimed.json()["error"] == "workspace_unclaimed"

            wrong_session = await client.post(
                "/v1/runner/claim-workspace",
                json={"session_id": "conv_other", "workspace": str(target)},
            )
            assert wrong_session.status_code == 403

            claimed = await client.post(
                "/v1/runner/claim-workspace",
                json={
                    "session_id": session_id,
                    "workspace": str(target),
                    "expected_workspace": str(staging),
                },
            )
            assert claimed.status_code == 200
            assert claimed.json()["workspace"] == str(target)
            assert Path.cwd() == target
            assert app.state.session_resource_registry._runner_workspace == target

            retry = await client.post(
                "/v1/runner/claim-workspace",
                json={
                    "session_id": session_id,
                    "workspace": str(target),
                    "expected_workspace": str(target),
                },
            )
            assert retry.status_code == 200
            different = await client.post(
                "/v1/runner/claim-workspace",
                json={"session_id": session_id, "workspace": str(staging)},
            )
            assert different.status_code == 409

            initialized = await client.post(
                "/v1/sessions", json={"session_id": session_id, "agent_id": "agent_test"}
            )
            assert initialized.status_code == 501  # No harness manager in this route test.
    finally:
        app.state.filesystem_registry.stop()


@pytest.mark.asyncio
async def test_unprepared_runner_rejects_workspace_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PRIMARY_SESSION_ID_ENV_VAR, "conv_normal")
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/runner/claim-workspace",
            json={"session_id": "conv_normal", "workspace": str(tmp_path)},
        )
    assert response.status_code == 404
