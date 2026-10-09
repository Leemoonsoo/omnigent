"""E2E: a real host daemon imports the whole history of Codex threads it reads from disk."""

from __future__ import annotations

import json
import signal
import sqlite3
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.e2e.test_host_e2e import _spawn_host_daemon, _wait_for_host_online

pytestmark = pytest.mark.timeout(300)

_CWD = "/repo"
_HOST_ENV_STRIP = ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "QWEN_HOME", "PI_CODING_AGENT_DIR")
_PARENT_TURNS = 10
_BIG_MESSAGE_COUNT = 425


def _message(role: str, text: str) -> dict[str, object]:
    content_type = "input_text" if role == "user" else "output_text"
    return {"type": "message", "role": role, "content": [{"type": content_type, "text": text}]}


class _Rollout:
    """One paginated Codex rollout; a rollout with ``history_base`` numbers on from its cutoff."""

    def __init__(self, thread_id: str, **meta: object) -> None:
        base = meta.get("history_base")
        self.next_ordinal = base["end_ordinal_exclusive"] if isinstance(base, dict) else 0
        self.records: list[dict[str, object]] = []
        self.append(
            "session_meta",
            {
                "id": thread_id,
                "cwd": _CWD,
                "source": "vscode",
                "history_mode": "paginated",
                **meta,
            },
        )

    def append(self, kind: str, payload: dict[str, object]) -> None:
        self.records.append({"ordinal": self.next_ordinal, "type": kind, "payload": payload})
        self.next_ordinal += 1

    def turn(self, index: int, user_text: str, assistant_text: str) -> None:
        self.append("turn_context", {"turn_id": f"turn_{index}", "cwd": _CWD})
        self.append("response_item", _message("user", user_text))
        self.append("response_item", _message("assistant", assistant_text))

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{json.dumps(r)}\n" for r in self.records), encoding="utf-8")


@dataclass(frozen=True)
class _Seeded:
    reverted: str
    metadata_only_fork: str
    compacted: str
    archived: str


def _seed_codex_home(home: Path) -> _Seeded:
    """Write a ``~/.codex`` holding the thread shapes an import must read in full."""
    codex = home / ".codex"
    sessions = codex / "sessions" / "2026" / "10" / "02"
    rows: list[tuple[str, str, str, int]] = []

    def name(thread_id: str, stamp: str, rollout_id: str | None = None) -> str:
        ids = thread_id if rollout_id is None else f"{thread_id}_{rollout_id}"
        return f"rollout-2026-10-02T{stamp}-{ids}.jsonl"

    # A thread reverted to its second turn: the thread store names the new
    # rollout, whose history_base points back into the original one.
    reverted = str(uuid.uuid4())
    original = _Rollout(reverted)
    for index in (1, 2, 3):
        original.turn(
            index, f"reverted thread question {index}", f"reverted thread answer {index}"
        )
    original.write(sessions / name(reverted, "09-00-00"))
    revert_point = 1 + 3 * 2
    current = _Rollout(
        reverted,
        history_base={"thread_id": reverted, "end_ordinal_exclusive": revert_point},
    )
    current.turn(3, "reverted thread question after revert", "reverted thread final answer")
    current_path = sessions / name(reverted, "09-30-00", str(uuid.uuid4()))
    current.write(current_path)
    rows.append((reverted, "reverted thread question 1", str(current_path), 0))

    # A fork that has not taken a turn yet holds only metadata.
    parent = str(uuid.uuid4())
    parent_rollout = _Rollout(parent)
    for index in range(1, _PARENT_TURNS + 1):
        parent_rollout.turn(index, f"fork parent question {index}", f"fork parent answer {index}")
    parent_path = sessions / name(parent, "10-00-00")
    parent_rollout.write(parent_path)
    rows.append((parent, "fork parent question 1", str(parent_path), 0))
    fork = str(uuid.uuid4())
    fork_rollout = _Rollout(
        fork,
        history_base={"thread_id": parent, "end_ordinal_exclusive": parent_rollout.next_ordinal},
    )
    fork_rollout.append("event_msg", {"type": "thread_settings_applied", "thread_id": fork})
    fork_path = sessions / name(fork, "10-30-00")
    fork_rollout.write(fork_path)
    rows.append((fork, "", str(fork_path), 0))

    # Above the 2 MiB threshold, with compactions after messages 100, 200 and 310.
    compacted = str(uuid.uuid4())
    big = _Rollout(compacted)
    padding = " lorem" * 1900
    for number in range(1, _BIG_MESSAGE_COUNT + 1):
        text = f"compacted thread message {number}{padding}"
        if number % 2 == 1:
            big.append("turn_context", {"turn_id": f"turn_{(number + 1) // 2}", "cwd": _CWD})
            big.append("response_item", _message("user", text))
        else:
            big.append("response_item", _message("assistant", text))
        if number in (100, 200, 310):
            summary = f"Summary of the first {number} messages."
            big.append(
                "compacted",
                {"message": summary, "replacement_history": [_message("user", summary)]},
            )
    big_path = sessions / name(compacted, "11-00-00")
    big.write(big_path)
    rows.append((compacted, "compacted thread message 1", str(big_path), 0))

    # `codex archive` moves the rollout under archived_sessions/ and sets threads.archived.
    archived = str(uuid.uuid4())
    archived_rollout = _Rollout(archived)
    archived_rollout.turn(1, "archived thread question", "archived thread answer")
    archived_path = codex / "archived_sessions" / name(archived, "12-00-00")
    archived_rollout.write(archived_path)
    rows.append((archived, "archived thread question", str(archived_path), 1))

    con = sqlite3.connect(codex / "state_5.sqlite")
    try:
        con.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, first_user_message TEXT, "
            "rollout_path TEXT, archived INTEGER)"
        )
        con.executemany(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
            [(thread, title, title, path, flag) for thread, title, path, flag in rows],
        )
        con.commit()
    finally:
        con.close()
    return _Seeded(reverted, fork, compacted, archived)


@dataclass(frozen=True)
class _ImportHost:
    host_id: str
    seeded: _Seeded


@pytest.fixture(scope="module")
def import_host(
    live_server: str,
    http_client: httpx.Client,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_ImportHost]:
    """A real host daemon whose ``HOME`` holds the seeded Codex threads."""
    home = tmp_path_factory.mktemp("codex-history-host")
    seeded = _seed_codex_home(home)
    with pytest.MonkeyPatch.context() as patch:
        for name in _HOST_ENV_STRIP:
            patch.delenv(name, raising=False)
        daemon = _spawn_host_daemon(
            tmp_path=home, live_server=live_server, mock_llm_server_url=mock_llm_server_url
        )
    try:
        _wait_for_host_online(http_client, daemon.host_id)
        yield _ImportHost(daemon.host_id, seeded)
    finally:
        daemon.proc.send_signal(signal.SIGTERM)
        try:
            daemon.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.proc.kill()
            daemon.proc.wait(timeout=5)


def _import(http_client: httpx.Client, host: _ImportHost, thread_id: str) -> str:
    """Import one Codex thread through the host; return the new session id."""
    response = http_client.post(
        "/v1/imports/local",
        json={"host_id": host.host_id, "source": "codex", "session_id": thread_id},
        timeout=120.0,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["imported"] == 1, result
    return result["sessions"][0]["session_id"]


def _message_texts(http_client: httpx.Client, session_id: str) -> list[str]:
    """Text of every visible message item, oldest first."""
    texts: list[str] = []
    after: str | None = None
    while True:
        params: dict[str, object] = {"limit": 1000, "order": "asc"}
        if after is not None:
            params["after"] = after
        response = http_client.get(f"/v1/sessions/{session_id}/items", params=params, timeout=30)
        response.raise_for_status()
        body = response.json()
        for item in body["data"]:
            if item.get("type") == "message" and not item.get("is_meta"):
                texts.extend(
                    block["text"]
                    for block in item.get("content") or []
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                )
        if not body.get("has_more") or not body["data"]:
            return texts
        after = body["data"][-1]["id"]


def test_import_keeps_history_from_before_a_revert(
    http_client: httpx.Client, import_host: _ImportHost
) -> None:
    """The thread store's current rollout plus the original rollout up to the revert point."""
    session_id = _import(http_client, import_host, import_host.seeded.reverted)

    assert _message_texts(http_client, session_id) == [
        "reverted thread question 1",
        "reverted thread answer 1",
        "reverted thread question 2",
        "reverted thread answer 2",
        "reverted thread question after revert",
        "reverted thread final answer",
    ]


def test_import_follows_history_base_for_metadata_only_fork(
    http_client: httpx.Client, import_host: _ImportHost
) -> None:
    """A fork that holds only metadata imports the history it inherits."""
    session_id = _import(http_client, import_host, import_host.seeded.metadata_only_fork)

    texts = _message_texts(http_client, session_id)
    assert texts[0] == "fork parent question 1"
    assert texts[-1] == f"fork parent answer {_PARENT_TURNS}"
    assert len(texts) == 2 * _PARENT_TURNS


def test_import_keeps_every_message_of_a_compacted_thread(
    http_client: httpx.Client, import_host: _ImportHost
) -> None:
    """A transcript above 2 MiB with several compactions imports every visible message."""
    session_id = _import(http_client, import_host, import_host.seeded.compacted)

    texts = _message_texts(http_client, session_id)
    assert len(texts) == _BIG_MESSAGE_COUNT, f"imported {len(texts)} of {_BIG_MESSAGE_COUNT}"
    assert texts[0].startswith("compacted thread message 1 ")


def test_import_keeps_a_codex_archived_thread_archived(
    http_client: httpx.Client, import_host: _ImportHost
) -> None:
    """A thread archived in Codex is imported as an archived session."""
    session_id = _import(http_client, import_host, import_host.seeded.archived)

    response = http_client.get(
        f"/v1/sessions/{session_id}",
        params={"include_items": "false", "include_liveness": "false"},
        timeout=30,
    )
    response.raise_for_status()
    assert response.json()["archived"] is True
