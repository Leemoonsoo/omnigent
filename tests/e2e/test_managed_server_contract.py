"""Portable OSS behavioral contract for local and managed servers."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from typing import Any

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e._managed_server import (
    ManagedServerAuth,
    ManagedServerConfig,
    managed_client,
)
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)


def _iter_sse(response: httpx.Response) -> Iterator[dict[str, Any]]:
    buffer = ""
    for chunk in response.iter_text():
        buffer += chunk
        while "\n\n" in buffer:
            frame, _, buffer = buffer.partition("\n\n")
            data_line = next(
                (line for line in frame.splitlines() if line.startswith("data:")), None
            )
            if data_line is None:
                continue
            payload = data_line.removeprefix("data:").strip()
            if payload == "[DONE]":
                return
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def _lookup_agent_id(client: httpx.Client, name: str) -> str:
    response = client.get("/v1/agents", params={"limit": 100})
    response.raise_for_status()
    agents = response.json().get("data", [])
    for agent in agents:
        if agent.get("name") == name:
            return str(agent["id"])
    raise AssertionError(
        f"managed agent {name!r} not found; available agents: "
        f"{[agent.get('name') for agent in agents]}"
    )


def _wait_for_managed_runner(
    client: httpx.Client,
    auth: ManagedServerAuth,
    session_id: str,
    *,
    timeout: float = 300,
) -> None:
    deadline = time.monotonic() + timeout
    last_snapshot: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/sessions/{session_id}")
        response.raise_for_status()
        last_snapshot = response.json()
        host_id = last_snapshot.get("host_id")
        if isinstance(host_id, str) and host_id:
            auth.remember_session_host(session_id, host_id)
        sandbox = last_snapshot.get("sandbox_status")
        if isinstance(sandbox, dict) and sandbox.get("stage") == "failed":
            raise AssertionError(f"managed sandbox launch failed: {sandbox.get('error')}")
        if host_id and last_snapshot.get("runner_id") and last_snapshot.get("runner_online"):
            return
        time.sleep(1)
    raise AssertionError(
        f"managed runner was not ready within {timeout}s; last snapshot: {last_snapshot}"
    )


def _run_streamed_turn(
    client: httpx.Client,
    poster: httpx.Client,
    session_id: str,
    prompt: str,
) -> tuple[list[str], str]:
    event_types: list[str] = []
    text_chunks: list[str] = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        with client.stream("GET", f"/v1/sessions/{session_id}/stream", timeout=300) as stream:
            stream.raise_for_status()
            send_future = None
            for event in _iter_sse(stream):
                if send_future is None:
                    send_future = executor.submit(
                        send_user_message_to_session,
                        poster,
                        session_id=session_id,
                        content=prompt,
                    )
                event_type = event.get("type")
                if isinstance(event_type, str):
                    event_types.append(event_type)
                if event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str):
                        text_chunks.append(delta)
                if event_type in {"response.completed", "response.failed"}:
                    break
            assert send_future is not None, "session stream ended before its first SSE frame"
            response_id = send_future.result(timeout=300)
    assert "response.completed" in event_types, f"turn did not complete; saw {event_types}"
    return event_types, response_id


def _wait_until_idle(client: httpx.Client, session_id: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        response = client.get(f"/v1/sessions/{session_id}")
        response.raise_for_status()
        last_status = response.json().get("status")
        if last_status == "idle":
            return
        if last_status == "failed":
            break
        time.sleep(0.25)
    raise AssertionError(f"session did not settle to idle; last status was {last_status!r}")


@pytest.mark.managed_black_box
def test_core_session_contract(request: pytest.FixtureRequest) -> None:
    """Create → turn → stream → items → permissions works in either server mode."""
    server_kind: str = request.config.getoption("--omnigent-server-kind")
    suffix = uuid.uuid4().hex[:8]
    prompt = f"Reply briefly to this managed contract probe ({suffix})."

    with ExitStack() as stack:
        if server_kind == "managed":
            config = ManagedServerConfig.from_pytest(request.config)
            auth = ManagedServerAuth(config)
            client = stack.enter_context(managed_client(config, auth))
            poster = stack.enter_context(managed_client(config, auth))
            agent_id = _lookup_agent_id(client, config.agent_name)
            created = client.post(
                "/v1/sessions",
                json={"agent_id": agent_id, "host_type": "managed"},
            )
            created.raise_for_status()
            session_id = str(created.json()["id"])
            _wait_for_managed_runner(client, auth, session_id)
        else:
            client = request.getfixturevalue("http_client")
            live_server: str = request.getfixturevalue("live_server")
            runner_id: str = request.getfixturevalue("live_runner_id")
            mock_url: str | None = request.getfixturevalue("mock_llm_server_url")
            model = f"managed-contract-{suffix}"
            reset_mock_llm(mock_url)
            configure_mock_llm(mock_url, [{"text": f"contract-ok-{suffix}"}], key=model)
            agent_name = register_inline_agent(
                client,
                name=f"managed-contract-{suffix}",
                harness="openai-agents",
                model=model,
                profile="",
                prompt="You are a terse test assistant.",
                mock_llm_base_url=f"{mock_url}/v1" if mock_url else None,
            )
            session_id = create_runner_bound_session(
                client, agent_name=agent_name, runner_id=runner_id
            )
            poster = stack.enter_context(
                httpx.Client(
                    base_url=live_server,
                    timeout=300,
                    headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
                )
            )

        try:
            event_types, _ = _run_streamed_turn(client, poster, session_id, prompt)
            assert event_types
            _wait_until_idle(client, session_id)

            items_response = client.get(
                f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}
            )
            items_response.raise_for_status()
            items = items_response.json().get("data", [])
            assert any(
                item.get("type") == "message" and item.get("role") == "user" for item in items
            )
            assert any(
                item.get("type") == "message" and item.get("role") == "assistant" for item in items
            )

            permissions_response = client.get(f"/v1/sessions/{session_id}/permissions")
            permissions_response.raise_for_status()
            permissions = permissions_response.json()
            assert isinstance(permissions.get("permissions"), list)
        finally:
            deleted = client.delete(f"/v1/sessions/{session_id}")
            deleted.raise_for_status()
