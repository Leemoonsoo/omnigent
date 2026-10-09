"""E2E: Settings › Import must keep the whole history of imported Codex threads.

Drives the real Settings › Import UI against a live host daemon whose
``$HOME/.codex`` mirrors Codex 0.154's paginated thread store: ``state_5.sqlite``
``threads`` rows (``rollout_path``, ``archived``, ``history_mode``) plus rollout
JSONL files under ``sessions/`` and ``archived_sessions/``. The seeded threads
cover a thread whose ``threads.rollout_path`` names a file the filename glob does
not match, a fork whose history lives in its parent via
``session_meta.history_base``, a multi-compaction transcript above 2 MiB, a thread
archived in Codex, and a batch size above 100.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]

_HOST_ID = "efbd3d053d2557bbaae25c7ea5bfeea0"
_HOST_NAME = "codex-history-import-host"
_CWD = "/repo"

_HOST_ENV_STRIP_PREFIXES = ("OMNIGENT_RUNNER_", "OMNIGENT_HOST_")
_HOST_ENV_STRIP = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "QWEN_HOME",
    "PI_CODING_AGENT_DIR",
    "OMNIGENT_CONFIG_HOME",
    "OMNIGENT_DATA_DIR",
    "RUNNER_SERVER_URL",
)

# First user messages double as the imported sessions' titles.
_TWO_FILE_TITLE = "codex-import two-file thread question 1"
_TWO_FILE_FINAL_ANSWER = "Final answer: this message only exists in the rollout_path file."
_PARENT_TITLE = "codex-import fork parent question 1"
_PARENT_TURNS = 10
_CHILD_TURN_TITLE = "codex-import fork child follow-up"
_CHILD_TURN_ANSWER = "Child answer after the fork."
_BIG_MESSAGE_COUNT = 425
_BIG_TITLE = "codex-import compacted thread message 1"
_ARCHIVED_TITLE = "codex-import archived thread question"
_PLAIN_TITLE = "codex-import plain thread question"

_THREADS_DDL = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    sandbox_policy TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    has_user_event INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    git_sha TEXT,
    git_branch TEXT,
    git_origin_url TEXT,
    cli_version TEXT NOT NULL DEFAULT '',
    first_user_message TEXT NOT NULL DEFAULT '',
    agent_nickname TEXT,
    agent_role TEXT,
    memory_mode TEXT NOT NULL DEFAULT 'enabled',
    model TEXT,
    reasoning_effort TEXT,
    agent_path TEXT,
    created_at_ms INTEGER,
    updated_at_ms INTEGER,
    thread_source TEXT,
    preview TEXT NOT NULL DEFAULT '',
    recency_at INTEGER NOT NULL DEFAULT 0,
    recency_at_ms INTEGER NOT NULL DEFAULT 0,
    history_mode TEXT NOT NULL DEFAULT 'legacy',
    name TEXT,
    is_pinned INTEGER NOT NULL DEFAULT 0,
    thread_section_id TEXT,
    section_position INTEGER,
    section_entered_at_ms INTEGER,
    project_id TEXT,
    originator TEXT,
    daybreak_enabled BOOLEAN
)
"""


def _message(role: str, text: str) -> dict[str, object]:
    content_type = "input_text" if role == "user" else "output_text"
    return {"type": "message", "role": role, "content": [{"type": content_type, "text": text}]}


class _Rollout:
    """Builder for one Codex rollout JSONL in the paginated record format."""

    def __init__(self, thread_id: str, started: str, **meta: object) -> None:
        self.thread_id = thread_id
        self.started = started
        self.records: list[dict[str, object]] = []
        self.append(
            "session_meta",
            {
                "session_id": thread_id,
                "id": thread_id,
                "timestamp": started,
                "cwd": _CWD,
                "originator": "codex_desktop",
                "cli_version": "0.154.0",
                "source": "vscode",
                "model_provider": "openai",
                "history_mode": "paginated",
                **meta,
            },
        )

    def append(self, kind: str, payload: dict[str, object]) -> None:
        self.records.append(
            {
                "timestamp": self.started,
                "ordinal": len(self.records),
                "type": kind,
                "payload": payload,
            }
        )

    def turn(self, index: int, user_text: str, assistant_text: str | None = None) -> None:
        self.append(
            "turn_context",
            {
                "turn_id": f"turn_{index}",
                "cwd": _CWD,
                "approval_policy": "never",
                "sandbox_policy": {"type": "read-only"},
                "model": "gpt-5",
            },
        )
        self.append("response_item", _message("user", user_text))
        if assistant_text is not None:
            self.append("response_item", _message("assistant", assistant_text))

    def compaction(self, summary: str, retained_user_messages: list[str]) -> None:
        # Shape of the ``compacted`` record Codex 0.154 writes after
        # ``thread/compact/start``: the summary plus retained user messages.
        self.append(
            "compacted",
            {
                "message": summary,
                "replacement_history": [
                    _message("user", text) for text in [*retained_user_messages, summary]
                ],
                "window_id": str(uuid.uuid4()),
            },
        )

    def write(self, path: Path, mtime: float) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for record in self.records:
                handle.write(json.dumps(record))
                handle.write("\n")
        os.utime(path, (mtime, mtime))
        return path.stat().st_size


@dataclass(frozen=True)
class _SeededThreads:
    two_file_thread: str
    two_file_current_rollout: str
    parent: str
    child_metadata_only: str
    child_with_turn: str
    compacted: str
    archived: str
    plain: str


def _thread_row(
    thread_id: str,
    rollout_path: Path,
    *,
    title: str,
    mtime: float,
    archived: bool = False,
) -> tuple[object, ...]:
    stamp = int(mtime)
    return (
        thread_id,
        str(rollout_path),
        stamp,
        stamp,
        "vscode",
        "openai",
        _CWD,
        title,
        "{}",
        "never",
        1 if archived else 0,
        stamp if archived else None,
        "0.154.0",
        title,
        "paginated",
    )


def _write_threads_db(codex_home: Path, rows: list[tuple[object, ...]]) -> None:
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute(_THREADS_DDL)
        con.executemany(
            "INSERT INTO threads (id, rollout_path, created_at, updated_at, source, "
            "model_provider, cwd, title, sandbox_policy, approval_mode, archived, archived_at, "
            "cli_version, first_user_message, history_mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        con.commit()
    finally:
        con.close()


def _seed_codex_home(home: Path) -> _SeededThreads:
    """Seed ``home/.codex`` with the reported Codex Desktop thread shapes."""
    codex_home = home / ".codex"
    sessions = codex_home / "sessions" / "2026" / "10" / "02"
    archived_dir = codex_home / "archived_sessions"
    now = time.time()
    rows: list[tuple[object, ...]] = []

    def new_id() -> str:
        return str(uuid.uuid4())

    def rollout_path(directory: Path, thread_id: str, stamp: str) -> Path:
        return directory / f"rollout-2026-10-02T{stamp}-{thread_id}.jsonl"

    # Facet 1: the current rollout named by threads.rollout_path does not carry
    # the thread id in its filename; a stale earlier file does.
    two_file = new_id()
    current_rollout_id = new_id()
    stale = _Rollout(two_file, "2026-10-02T09:00:00.000Z")
    current = _Rollout(two_file, "2026-10-02T09:00:00.000Z")
    for builder, turns in ((stale, 3), (current, 4)):
        for index in range(1, turns + 1):
            builder.turn(
                index,
                f"codex-import two-file thread question {index}",
                f"Two-file thread answer {index}." if index < 4 else _TWO_FILE_FINAL_ANSWER,
            )
    stale.write(rollout_path(sessions, two_file, "09-00-00"), now - 900)
    current_path = rollout_path(sessions, current_rollout_id, "09-30-00")
    current.write(current_path, now - 800)
    rows.append(_thread_row(two_file, current_path, title=_TWO_FILE_TITLE, mtime=now - 800))

    # Facet 2: a fork child inherits the parent's history through history_base.
    parent = new_id()
    parent_rollout = _Rollout(parent, "2026-10-02T10:00:00.000Z")
    for index in range(1, _PARENT_TURNS + 1):
        parent_rollout.turn(
            index, f"codex-import fork parent question {index}", f"Parent answer {index}."
        )
    parent_path = rollout_path(sessions, parent, "10-00-00")
    parent_size = parent_rollout.write(parent_path, now - 700)
    rows.append(_thread_row(parent, parent_path, title=_PARENT_TITLE, mtime=now - 700))
    fork_meta = {
        "forked_from_id": parent,
        "forked_from_ordinal_exclusive": len(parent_rollout.records),
        "history_base": {
            "thread_id": parent,
            "end_ordinal_exclusive": len(parent_rollout.records),
            "end_byte_offset": parent_size,
        },
    }
    child_meta_only = new_id()
    child_meta_rollout = _Rollout(child_meta_only, "2026-10-02T10:30:00.000Z", **fork_meta)
    child_meta_rollout.append(
        "event_msg",
        {"type": "thread_settings_applied", "thread_id": child_meta_only, "thread_settings": {}},
    )
    child_meta_path = rollout_path(sessions, child_meta_only, "10-30-00")
    child_meta_rollout.write(child_meta_path, now - 600)
    rows.append(_thread_row(child_meta_only, child_meta_path, title="", mtime=now - 600))

    child_with_turn = new_id()
    child_turn_rollout = _Rollout(child_with_turn, "2026-10-02T10:40:00.000Z", **fork_meta)
    child_turn_rollout.turn(1, _CHILD_TURN_TITLE, _CHILD_TURN_ANSWER)
    child_turn_path = rollout_path(sessions, child_with_turn, "10-40-00")
    child_turn_rollout.write(child_turn_path, now - 500)
    rows.append(
        _thread_row(child_with_turn, child_turn_path, title=_CHILD_TURN_TITLE, mtime=now - 500)
    )

    # Facet 3: a transcript above the 2 MiB import threshold with several
    # compactions; 425 visible messages, the last compaction after message 310.
    compacted = new_id()
    big = _Rollout(compacted, "2026-10-02T11:00:00.000Z")
    padding = " lorem" * 1900
    user_texts: list[str] = []
    for number in range(1, _BIG_MESSAGE_COUNT + 1):
        text = f"codex-import compacted thread message {number}{padding}"
        if number % 2 == 1:
            user_texts.append(text)
            big.append(
                "turn_context",
                {"turn_id": f"turn_{(number + 1) // 2}", "cwd": _CWD, "model": "gpt-5"},
            )
            big.append("response_item", _message("user", text))
        else:
            big.append("response_item", _message("assistant", text))
        if number in (100, 200, 310):
            big.compaction(
                f"Summary of the first {number} messages produced by the model.",
                user_texts[-2:],
            )
    compacted_path = rollout_path(sessions, compacted, "11-00-00")
    big.write(compacted_path, now - 400)
    rows.append(_thread_row(compacted, compacted_path, title=_BIG_TITLE, mtime=now - 400))

    # Facet 5: ``codex archive`` moves the rollout under archived_sessions/ and
    # sets threads.archived = 1.
    archived = new_id()
    archived_rollout = _Rollout(archived, "2026-10-02T12:00:00.000Z")
    archived_rollout.turn(1, _ARCHIVED_TITLE, "Archived thread answer.")
    archived_path = rollout_path(archived_dir, archived, "12-00-00")
    archived_rollout.write(archived_path, now - 300)
    rows.append(
        _thread_row(archived, archived_path, title=_ARCHIVED_TITLE, mtime=now - 300, archived=True)
    )

    plain = new_id()
    plain_rollout = _Rollout(plain, "2026-10-02T13:00:00.000Z")
    plain_rollout.turn(1, _PLAIN_TITLE, "Plain thread answer.")
    plain_path = rollout_path(sessions, plain, "13-00-00")
    plain_rollout.write(plain_path, now - 200)
    rows.append(_thread_row(plain, plain_path, title=_PLAIN_TITLE, mtime=now - 200))

    _write_threads_db(codex_home, rows)
    return _SeededThreads(
        two_file_thread=two_file,
        two_file_current_rollout=current_rollout_id,
        parent=parent,
        child_metadata_only=child_meta_only,
        child_with_turn=child_with_turn,
        compacted=compacted,
        archived=archived,
        plain=plain,
    )


@dataclass
class _ImportHost:
    host_id: str
    host_name: str
    proc: subprocess.Popen[bytes]
    daemon_log: Path
    seeded: _SeededThreads


def _wait_for_host_online(live_server: str, host_id: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{live_server}/v1/hosts", timeout=5)
            if resp.status_code == 200:
                last = resp.json()
                hosts = last.get("hosts", []) if isinstance(last, dict) else []
                if any(h.get("host_id") == host_id and h.get("status") == "online" for h in hosts):
                    return
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise RuntimeError(f"host {host_id} never came online; last /v1/hosts: {last!r}")


def start_import_host(live_server: str, home: Path) -> _ImportHost:
    """Seed ``home`` and start a real host daemon against ``live_server``."""
    seeded = _seed_codex_home(home)
    omni_dir = home / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    (omni_dir / "config.yaml").write_text(
        json.dumps({"host": {"host_id": _HOST_ID, "name": _HOST_NAME}}), encoding="utf-8"
    )
    env = {**os.environ}
    for key in list(env):
        if key in _HOST_ENV_STRIP or key.startswith(_HOST_ENV_STRIP_PREFIXES):
            env.pop(key, None)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"
    daemon_log = home / "host-daemon.log"
    with daemon_log.open("w") as log_handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    try:
        _wait_for_host_online(live_server, _HOST_ID)
    except RuntimeError as exc:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        raise RuntimeError(f"{exc}; daemon log tail: {daemon_log.read_text()[-2000:]}") from exc
    return _ImportHost(
        host_id=_HOST_ID, host_name=_HOST_NAME, proc=proc, daemon_log=daemon_log, seeded=seeded
    )


def stop_import_host(host: _ImportHost) -> None:
    host.proc.send_signal(signal.SIGTERM)
    try:
        host.proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        host.proc.kill()
        host.proc.wait(timeout=10)


@pytest.fixture(scope="module")
def import_host(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_ImportHost]:
    host = start_import_host(live_server, tmp_path_factory.mktemp("codex_import_host_home"))
    yield host
    stop_import_host(host)


def _choose(page: Page, select_test_id: str, option: str) -> None:
    page.get_by_test_id(select_test_id).click()
    page.get_by_role("option", name=option, exact=True).click()


def _open_import_panel(page: Page, live_server: str, host: _ImportHost) -> None:
    page.goto(f"{live_server}/settings/import")
    expect(page.get_by_test_id("import-sessions-panel")).to_be_visible(timeout=30_000)
    # A connected host surfaces a one-time "Your setup is ready" dialog that
    # would intercept the picker clicks.
    setup_ready = page.get_by_role("dialog").filter(has_text="Your setup is ready")
    if setup_ready.count() and setup_ready.first.is_visible():
        page.keyboard.press("Escape")
        expect(setup_ready.first).to_be_hidden(timeout=10_000)
    page.get_by_test_id("import-host-select").click()
    page.get_by_role("option").filter(has_text=host.host_name).click()


def _submit_and_wait(page: Page, timeout: int = 120_000) -> None:
    page.get_by_test_id("import-submit").click()
    result = page.get_by_test_id("import-result")
    error = page.get_by_test_id("import-error")
    expect(result.or_(error)).to_be_visible(timeout=timeout)


def _import_codex_thread_by_id(page: Page, thread_id: str) -> str:
    """Import one Codex thread via Session by ID and return the new session id."""
    _choose(page, "import-mode-select", "Session by ID")
    _choose(page, "import-source-select", "Codex")
    page.get_by_test_id("import-session-id").fill(thread_id)
    _submit_and_wait(page)
    error = page.get_by_test_id("import-error")
    assert not error.is_visible(), (
        f"import of {thread_id} failed wholesale: {error.inner_text()!r}"
    )
    failures = page.get_by_test_id("import-failure-item")
    assert failures.count() == 0, (
        f"import of {thread_id} was reported as failed: {failures.all_inner_texts()!r}"
    )
    link = page.get_by_test_id("import-result-sessions").get_by_role("link").first
    expect(link).to_be_visible()
    href = link.get_attribute("href") or ""
    match = re.search(r"/c/([^/?#]+)", href)
    assert match is not None, f"unexpected imported session link: {href!r}"
    return match.group(1)


def _visible_messages(live_server: str, session_id: str) -> list[str]:
    """Return the text of every non-meta message item, in transcript order."""
    texts: list[str] = []
    after: str | None = None
    while True:
        params: dict[str, object] = {"limit": 1000}
        if after is not None:
            params["after"] = after
        resp = httpx.get(
            f"{live_server}/v1/sessions/{session_id}/items", params=params, timeout=30
        )
        resp.raise_for_status()
        body = resp.json()
        for item in body["data"]:
            if item.get("type") != "message" or item.get("is_meta"):
                continue
            texts.extend(
                block["text"]
                for block in item.get("content") or []
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
        if not body.get("has_more") or not body["data"]:
            return texts
        after = body["data"][-1]["id"]


def _session(live_server: str, session_id: str) -> dict[str, object]:
    resp = httpx.get(
        f"{live_server}/v1/sessions/{session_id}",
        params={"include_items": "false", "include_liveness": "false"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


@pytest.mark.timeout(300)
def test_import_by_id_reads_rollout_path_target(
    request: pytest.FixtureRequest,
    live_server: str,
    import_host: _ImportHost,
) -> None:
    """The imported transcript comes from the file named by threads.rollout_path."""
    page: Page = request.getfixturevalue("page")
    _open_import_panel(page, live_server, import_host)
    session_id = _import_codex_thread_by_id(page, import_host.seeded.two_file_thread)
    page.get_by_test_id(f"import-result-link-{session_id}").click()

    bubbles = page.get_by_test_id("message-bubble")
    expect(bubbles.filter(has_text=_TWO_FILE_TITLE).first).to_be_visible(timeout=30_000)
    texts = _visible_messages(live_server, session_id)
    assert _TWO_FILE_FINAL_ANSWER in texts, (
        f"imported {len(texts)} messages ending with {texts[-1]!r}; the rollout_path file "
        "holding the final answer was not read"
    )
    expect(bubbles.filter(has_text=_TWO_FILE_FINAL_ANSWER).first).to_be_visible(timeout=10_000)


@pytest.mark.timeout(300)
def test_import_by_id_follows_history_base_for_fork_with_own_turn(
    request: pytest.FixtureRequest,
    live_server: str,
    import_host: _ImportHost,
) -> None:
    """A forked thread imports the parent's inherited history before its own turn."""
    page: Page = request.getfixturevalue("page")
    _open_import_panel(page, live_server, import_host)
    session_id = _import_codex_thread_by_id(page, import_host.seeded.child_with_turn)
    page.get_by_test_id(f"import-result-link-{session_id}").click()

    bubbles = page.get_by_test_id("message-bubble")
    expect(bubbles.filter(has_text=_CHILD_TURN_ANSWER).first).to_be_visible(timeout=30_000)
    texts = _visible_messages(live_server, session_id)
    assert _PARENT_TITLE in texts, (
        f"forked thread imported only its own {len(texts)} messages; the history_base "
        f"parent ({_PARENT_TURNS} turns) was not followed"
    )
    expect(bubbles.filter(has_text=_PARENT_TITLE).first).to_be_visible(timeout=10_000)


@pytest.mark.timeout(300)
def test_import_by_id_follows_history_base_for_metadata_only_fork(
    request: pytest.FixtureRequest,
    live_server: str,
    import_host: _ImportHost,
) -> None:
    """A fork that holds only metadata still imports its inherited history."""
    page: Page = request.getfixturevalue("page")
    _open_import_panel(page, live_server, import_host)
    session_id = _import_codex_thread_by_id(page, import_host.seeded.child_metadata_only)
    page.get_by_test_id(f"import-result-link-{session_id}").click()

    bubbles = page.get_by_test_id("message-bubble")
    expect(bubbles.filter(has_text=_PARENT_TITLE).first).to_be_visible(timeout=30_000)
    assert _PARENT_TITLE in _visible_messages(live_server, session_id)


@pytest.mark.timeout(300)
def test_import_by_id_keeps_full_history_of_compacted_thread(
    request: pytest.FixtureRequest,
    live_server: str,
    import_host: _ImportHost,
) -> None:
    """A large multi-compaction transcript imports every visible message."""
    page: Page = request.getfixturevalue("page")
    _open_import_panel(page, live_server, import_host)
    session_id = _import_codex_thread_by_id(page, import_host.seeded.compacted)
    page.get_by_test_id(f"import-result-link-{session_id}").click()

    bubbles = page.get_by_test_id("message-bubble")
    expect(bubbles.first).to_be_visible(timeout=30_000)
    page.keyboard.press("Home")
    texts = _visible_messages(live_server, session_id)
    assert len(texts) == _BIG_MESSAGE_COUNT and texts[0].startswith(_BIG_TITLE), (
        f"imported {len(texts)} of {_BIG_MESSAGE_COUNT} messages; transcript now starts with "
        f"{texts[0][:80]!r}"
    )


@pytest.mark.timeout(300)
def test_imported_archived_codex_thread_stays_archived(
    request: pytest.FixtureRequest,
    live_server: str,
    import_host: _ImportHost,
) -> None:
    """A thread archived in Codex lands under Settings › Archived sessions."""
    page: Page = request.getfixturevalue("page")
    _open_import_panel(page, live_server, import_host)
    session_id = _import_codex_thread_by_id(page, import_host.seeded.archived)

    page.goto(f"{live_server}/settings/archived")
    expect(page.get_by_test_id("archived-retention")).to_be_visible(timeout=30_000)
    archived_row = page.get_by_test_id("archived-row").filter(has_text=_ARCHIVED_TITLE)
    assert _session(live_server, session_id)["archived"] is True, (
        "the Codex-archived thread was imported as an active session"
    )
    expect(archived_row).to_be_visible(timeout=30_000)


@pytest.mark.timeout(300)
def test_recent_import_accepts_batch_size_above_100(
    request: pytest.FixtureRequest,
    live_server: str,
    import_host: _ImportHost,
) -> None:
    """Choosing 'Last 200' imports recent Codex threads instead of a limit error."""
    page: Page = request.getfixturevalue("page")
    _open_import_panel(page, live_server, import_host)
    _choose(page, "import-source-select", "Codex")
    _choose(page, "import-limit-select", "Last 200")
    _submit_and_wait(page)

    error = page.get_by_test_id("import-error")
    assert not error.is_visible(), f"batch import rejected: {error.inner_text()!r}"
    expect(page.get_by_test_id("import-result")).to_contain_text(re.compile(r"Imported \d+"))
    expect(page.get_by_test_id("import-result-sessions")).to_contain_text(_PLAIN_TITLE)
