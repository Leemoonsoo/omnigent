"""Managed-server transport helpers for black-box E2E tests."""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Generator
from dataclasses import dataclass
from typing import Literal

import httpx
import pytest

_ORG_HEADER = "X-Databricks-Org-Id"
_SLICE_HEADER = "X-Databricks-Omnigent-Slice-Key"
_TRAFFIC_HEADER = "x-databricks-traffic-id"
_SESSION_PATH = re.compile(r"/v1/sessions/([^/]+)(?:/|$)")


@dataclass(frozen=True)
class ManagedServerConfig:
    """Connection settings for a managed Omnigent deployment."""

    base_url: str
    token: str
    org_id: str | None
    org_routing: Literal["header", "query"]
    traffic_id: str | None
    agent_name: str

    @classmethod
    def from_pytest(cls, config: pytest.Config) -> ManagedServerConfig:
        """Build configuration from non-secret CLI options and a token env var."""
        base_url = config.getoption("--omnigent-server-url")
        if not base_url:
            raise pytest.UsageError("managed mode requires --omnigent-server-url")

        token_env = config.getoption("--omnigent-token-env")
        token = os.environ.get(token_env)
        if not token:
            raise pytest.UsageError(
                f"managed mode requires a bearer token in environment variable {token_env!r}"
            )

        return cls(
            base_url=base_url.rstrip("/"),
            token=token,
            org_id=config.getoption("--omnigent-org-id"),
            org_routing=config.getoption("--omnigent-org-routing"),
            traffic_id=config.getoption("--omnigent-traffic-id"),
            agent_name=config.getoption("--omnigent-managed-agent"),
        )


class ManagedServerAuth(httpx.Auth):
    """Inject managed routing headers and retry wrong-replica responses."""

    requires_response_body = True

    def __init__(self, config: ManagedServerConfig) -> None:
        self._config = config
        self._session_hosts: dict[str, str] = {}
        self._keyless_hosts: set[str] = set()
        self._lock = threading.Lock()

    def remember_session_host(self, session_id: str, host_id: str) -> None:
        """Associate a session with the host that owns its runner tunnel."""
        with self._lock:
            self._session_hosts[session_id] = host_id

    def is_host_keyless(self, host_id: str) -> bool:
        """Return whether a successful retry proved that *host_id* routes keyless."""
        with self._lock:
            return host_id in self._keyless_hosts

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """Apply auth/routing and yield at most one keyless retry."""
        request.headers["Authorization"] = f"Bearer {self._config.token}"
        if self._config.traffic_id:
            request.headers[_TRAFFIC_HEADER] = self._config.traffic_id
        if self._config.org_id:
            if self._config.org_routing == "header":
                request.headers[_ORG_HEADER] = self._config.org_id
            else:
                request.url = request.url.copy_merge_params({"o": self._config.org_id})

        host_id = self._host_for_request(request)
        stamped_slice_key = False
        if host_id and not self.is_host_keyless(host_id):
            request.headers[_SLICE_HEADER] = host_id
            stamped_slice_key = True

        response = yield request
        if stamped_slice_key and self._is_wrong_replica(response):
            request.headers.pop(_SLICE_HEADER, None)
            response = yield request
            if host_id and response.is_success:
                with self._lock:
                    self._keyless_hosts.add(host_id)
        elif host_id and not stamped_slice_key and self._is_wrong_replica(response):
            with self._lock:
                self._keyless_hosts.discard(host_id)

    def _host_for_request(self, request: httpx.Request) -> str | None:
        match = _SESSION_PATH.search(request.url.path)
        if match is None:
            return None
        session_id = match.group(1)
        with self._lock:
            return self._session_hosts.get(session_id)

    @staticmethod
    def _is_wrong_replica(response: httpx.Response) -> bool:
        if response.status_code != 400:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        if not isinstance(body, dict):
            return False
        error = body.get("error")
        return isinstance(error, dict) and error.get("code") == "wrong_replica"


def managed_client(
    config: ManagedServerConfig,
    auth: ManagedServerAuth,
    *,
    timeout: float = 300,
) -> httpx.Client:
    """Create a client sharing the managed routing state in *auth*."""
    return httpx.Client(base_url=config.base_url, auth=auth, timeout=timeout)
