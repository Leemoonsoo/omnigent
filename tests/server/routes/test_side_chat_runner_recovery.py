"""Side chats recover their shared runner through their source session."""

import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ
from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import orchestration
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_HOST_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
_OWNER = "alice@example.com"
_OTHER = "bob@example.com"


def _source_and_side_chat(
    store: SqlAlchemyConversationStore, db_uri: str
) -> tuple[Conversation, Conversation]:
    """Create a host-bound source and a side chat still bound to its old runner."""
    agent_id = generate_agent_id()
    SqlAlchemyAgentStore(db_uri).create(agent_id, name="test", bundle_location="test:///bundle")
    source = store.create_conversation(agent_id=agent_id)
    store.set_host_id(source.id, _HOST_ID, workspace="/workspace")
    store.set_runner_id(source.id, "runner-replacement")
    side = store.fork_conversation(
        source.id,
        extra_labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: source.id},
    )
    store.set_runner_id(side.id, "runner-exited")
    saved_source = store.get_conversation(source.id)
    saved_side = store.get_conversation(side.id)
    assert saved_source is not None and saved_side is not None
    return saved_source, saved_side


async def _recover(
    store: SqlAlchemyConversationStore,
    side: Conversation,
    *,
    user_id: str | None = None,
    permission_store: SqlAlchemyPermissionStore | None = None,
    runner_router: Mock | None = None,
) -> tuple[httpx.AsyncClient | None, Conversation]:
    return await orchestration._recover_side_chat_runner_via_source(
        side,
        app_state=SimpleNamespace(),
        conversation_store=store,
        runner_router=runner_router,
        user_id=user_id,
        permission_store=permission_store,
    )


async def test_side_chat_rebinds_to_source_runner(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    source, side = _source_and_side_chat(store, db_uri)
    runner_client = httpx.AsyncClient(base_url="http://runner")
    ensure = AsyncMock(return_value=(runner_client, source))
    monkeypatch.setattr(orchestration, "ensure_runner_connected", ensure)
    monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=runner_client))

    try:
        client, recovered = await _recover(store, side)
    finally:
        await runner_client.aclose()

    assert client is runner_client
    assert recovered.runner_id == "runner-replacement"
    assert ensure.await_args.kwargs["session_id"] == source.id
    saved = store.get_conversation(side.id)
    assert saved is not None and saved.runner_id == "runner-replacement"
    assert saved.host_id is None


@pytest.mark.parametrize("source_level", [None, LEVEL_READ])
async def test_side_chat_recovery_requires_edit_access_to_source(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, source_level: int | None
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    source, side = _source_and_side_chat(store, db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in (_OWNER, _OTHER):
        permissions.ensure_user(user)
    permissions.grant(_OWNER, source.id, LEVEL_OWNER)
    permissions.grant(_OTHER, side.id, LEVEL_EDIT)
    if source_level is not None:
        permissions.grant(_OTHER, source.id, source_level)
    ensure = AsyncMock()
    monkeypatch.setattr(orchestration, "ensure_runner_connected", ensure)

    client, unchanged = await _recover(store, side, user_id=_OTHER, permission_store=permissions)

    assert client is None and unchanged is side
    ensure.assert_not_awaited()
    saved = store.get_conversation(side.id)
    assert saved is not None and saved.runner_id == "runner-exited"


async def test_side_chat_binding_unchanged_when_source_runner_unavailable(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    source, side = _source_and_side_chat(store, db_uri)
    ensure = AsyncMock(return_value=(None, source))
    monkeypatch.setattr(orchestration, "ensure_runner_connected", ensure)

    client, unchanged = await _recover(store, side)

    assert client is None and unchanged is side
    ensure.assert_awaited_once()
    saved = store.get_conversation(side.id)
    assert saved is not None and saved.runner_id == "runner-exited"


async def test_side_chat_recovery_redirects_when_source_host_is_on_another_replica(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    _, side = _source_and_side_chat(store, db_uri)
    router = Mock()
    router.host_is_on_another_replica.return_value = True
    ensure = AsyncMock()
    monkeypatch.setattr(orchestration, "ensure_runner_connected", ensure)

    with pytest.raises(OmnigentError) as raised:
        await _recover(store, side, runner_router=router)

    assert raised.value.code == ErrorCode.WRONG_REPLICA
    router.host_is_on_another_replica.assert_called_once_with(_HOST_ID)
    ensure.assert_not_awaited()
    saved = store.get_conversation(side.id)
    assert saved is not None and saved.runner_id == "runner-exited"


@pytest.mark.parametrize(
    "shape", ["host_bound", "missing_source_label", "not_side_chat", "sub_agent"]
)
async def test_only_hostless_side_chats_recover_through_source(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    source, side = _source_and_side_chat(store, db_uri)
    labels = dict(side.labels)
    changes: dict[str, object] = {}
    if shape == "host_bound":
        changes["host_id"] = _HOST_ID
    elif shape == "missing_source_label":
        labels.pop(SIDE_CHAT_SOURCE_LABEL_KEY)
    elif shape == "not_side_chat":
        labels.pop(SIDE_CHAT_LABEL_KEY)
    else:
        changes["kind"] = "sub_agent"
    candidate = dataclasses.replace(side, **changes, labels=labels)
    ensure = AsyncMock(return_value=(object(), source))
    monkeypatch.setattr(orchestration, "ensure_runner_connected", ensure)

    client, unchanged = await _recover(store, candidate)

    assert client is None and unchanged is candidate
    ensure.assert_not_awaited()
