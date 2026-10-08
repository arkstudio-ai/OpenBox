"""Fresh assistant authority checks use one query without retained ORM authority."""
import pytest
from sqlalchemy import event, func, select

from assistant.commands import _authority
from assistant.policy import AssistantError
from assistant.service import ensure_main_session
from db.base import get_db_session, get_engine
from db.models.agent_event import AgentEvent
from db.models.session import Session
from db.models.workspace import Workspace, WorkspaceMember
from session.agent_event_log import append_agent_event_locked, prepare_agent_event_write
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


async def authority_scope():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    return dict(user_id=owner, workspace_id=workspace, main_id=main.id), other


async def test_authority_uses_one_fresh_query_per_check():
    scope, _ = await authority_scope()
    statements = []
    def count(*args):
        statements.append(args[2])
    engine = get_engine().sync_engine
    async with get_db_session() as db:
        event.listen(engine, "before_cursor_execute", count)
        try:
            first = await _authority(db, **scope)
            second = await _authority(db, **scope)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert first is second and first.id == scope["main_id"]
        assert len(statements) == 2, len(statements)
        assert all(statement.lstrip().startswith("SELECT") for statement in statements)


@pytest.mark.parametrize("change", ["member", "workspace", "main", "owner", "kind", "visibility", "memory", "deleted"])
async def test_authority_retains_membership_and_private_main_scope(change):
    scope, other = await authority_scope()
    expected = "ASSISTANT_UNAVAILABLE"
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
        if change == "member":
            (await db.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))).status = "removed"
            expected = "ASSISTANT_WORKSPACE_FORBIDDEN"
        elif change == "workspace":
            (await db.get(Workspace, scope["workspace_id"])).is_deleted = True
            expected = "ASSISTANT_WORKSPACE_FORBIDDEN"
        elif change == "main":
            scope = scope | {"main_id": "unavailable-main"}
        elif change == "owner":
            scope = scope | {"user_id": other}
        else:
            name, value = {"kind": ("kind", "normal"), "visibility": ("visibility", "workspace"),
                           "memory": ("memory_policy", "standard"), "deleted": ("is_deleted", True)}[change]
            setattr(main, name, value)
    async with get_db_session() as db:
        with pytest.raises(AssistantError) as denied:
            await _authority(db, **scope)
        assert denied.value.code == expected


async def test_membership_refusal_precedes_missing_main_and_pending_changes_flush():
    scope, _ = await authority_scope()
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
        main.title = "Preserved pending title"
        assert (await _authority(db, **scope)).title == "Preserved pending title"
        member = await db.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))
        member.status = "removed"
        with pytest.raises(AssistantError) as denied:
            await _authority(db, **(scope | {"main_id": "unavailable-main"}))
        assert denied.value.code == "ASSISTANT_WORKSPACE_FORBIDDEN"
    async with get_db_session() as db:
        assert (await db.get(Session, scope["main_id"])).title == "Preserved pending title"


async def test_authority_keeps_pending_locked_main_changes_with_autoflush_disabled():
    scope, _ = await authority_scope()
    async with get_db_session() as db:
        main = await prepare_agent_event_write(db, session_id=scope["main_id"], user_id=scope["user_id"],
                                              run_fence=None)
        sequence = await db.scalar(select(func.coalesce(func.max(AgentEvent.sequence), 0))
            .where(AgentEvent.session_id == main.id))
        with db.no_autoflush:
            main.title = "Unflushed locked title"
            main.tool_exposure_state = {"version": 1, "revision": 9}
            assert await _authority(db, **scope) is main
            assert main.title == "Unflushed locked title"
            assert main.tool_exposure_state == {"version": 1, "revision": 9}
        first = await append_agent_event_locked(db, main, kind="assistant.authority.tested", payload={"step": 1})
        assert await _authority(db, **scope) is main
        second = await append_agent_event_locked(db, main, kind="assistant.authority.tested", payload={"step": 2})
        assert (first.sequence, second.sequence) == (sequence + 1, sequence + 2)
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
        assert main.title == "Unflushed locked title"
        assert main.tool_exposure_state == {"version": 1, "revision": 9}


async def test_held_main_rechecks_memory_isolation_after_independent_revocation():
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent READ COMMITTED readers require PostgreSQL")
    scope, _ = await authority_scope()
    async with get_db_session() as db:
        held = await _authority(db, **scope)
        assert held.memory_policy == "assistant_isolated"
        async with get_db_session() as writer:
            (await writer.get(Session, scope["main_id"])).memory_policy = "standard"
        with pytest.raises(AssistantError) as denied:
            await _authority(db, **scope)
        assert denied.value.code == "ASSISTANT_UNAVAILABLE"
