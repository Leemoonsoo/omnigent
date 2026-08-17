"""Tests for managed black-box E2E request routing."""

from __future__ import annotations

from typing import Literal

import httpx

from tests.e2e._managed_server import ManagedServerAuth, ManagedServerConfig


def _config(*, org_routing: Literal["header", "query"] = "header") -> ManagedServerConfig:
    return ManagedServerConfig(
        base_url="https://workspace.example/api/2.0/omnigent",
        token="secret-token",
        org_id="12345",
        org_routing=org_routing,
        traffic_id="testenv://liteswap/contract",
        agent_name="databricks_coding_agent",
    )


def test_injects_auth_org_and_liteswap_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    auth = ManagedServerAuth(_config())
    with httpx.Client(
        base_url=_config().base_url,
        auth=auth,
        transport=httpx.MockTransport(handler),
    ) as client:
        client.get("/v1/agents")

    assert seen[0].headers["Authorization"] == "Bearer secret-token"
    assert seen[0].headers["X-Databricks-Org-Id"] == "12345"
    assert seen[0].headers["x-databricks-traffic-id"] == "testenv://liteswap/contract"


def test_supports_org_query_routing() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    config = _config(org_routing="query")
    with httpx.Client(
        base_url=config.base_url,
        auth=ManagedServerAuth(config),
        transport=httpx.MockTransport(handler),
    ) as client:
        client.get("/v1/agents", params={"limit": 10})

    assert seen[0].url.params["limit"] == "10"
    assert seen[0].url.params["o"] == "12345"
    assert "X-Databricks-Org-Id" not in seen[0].headers


def test_managed_create_is_unkeyed_then_session_calls_use_host() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    config = _config()
    auth = ManagedServerAuth(config)
    with httpx.Client(
        base_url=config.base_url,
        auth=auth,
        transport=httpx.MockTransport(handler),
    ) as client:
        client.post("/v1/sessions", json={"host_type": "managed"})
        auth.remember_session_host("conv_1", "host_1")
        client.get("/v1/sessions/conv_1/items")

    assert "X-Databricks-Omnigent-Slice-Key" not in seen[0].headers
    assert seen[1].headers["X-Databricks-Omnigent-Slice-Key"] == "host_1"


def test_wrong_replica_retries_once_and_sticks_to_keyless() -> None:
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers.copy())
        if len(seen_headers) == 1:
            return httpx.Response(
                400,
                json={"error": {"code": "wrong_replica", "message": "retry"}},
            )
        return httpx.Response(200, json={"data": []})

    config = _config()
    auth = ManagedServerAuth(config)
    auth.remember_session_host("conv_1", "host_1")
    with httpx.Client(
        base_url=config.base_url,
        auth=auth,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.get("/v1/sessions/conv_1/items").status_code == 200
        assert client.get("/v1/sessions/conv_1/permissions").status_code == 200

    assert seen_headers[0]["X-Databricks-Omnigent-Slice-Key"] == "host_1"
    assert "X-Databricks-Omnigent-Slice-Key" not in seen_headers[1]
    assert "X-Databricks-Omnigent-Slice-Key" not in seen_headers[2]
    assert auth.is_host_keyless("host_1")


def test_failed_keyless_retry_does_not_demote_host() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(400, json={"error": {"code": "wrong_replica"}})
        return httpx.Response(503, json={"error": {"code": "runner_unavailable"}})

    config = _config()
    auth = ManagedServerAuth(config)
    auth.remember_session_host("conv_1", "host_1")
    with httpx.Client(
        base_url=config.base_url,
        auth=auth,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.get("/v1/sessions/conv_1/items").status_code == 503

    assert not auth.is_host_keyless("host_1")


def test_non_object_error_body_does_not_trigger_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": "wrong_replica"})

    config = _config()
    auth = ManagedServerAuth(config)
    auth.remember_session_host("conv_1", "host_1")
    with httpx.Client(
        base_url=config.base_url,
        auth=auth,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.get("/v1/sessions/conv_1/items").status_code == 400

    assert calls == 1
