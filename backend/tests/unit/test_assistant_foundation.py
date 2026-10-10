"""Real SQL coverage for fixed-entry concurrency and private session audiences."""
import asyncio
from datetime import datetime, timezone
import os
from uuid import uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url

from api import sessions as routes
from assistant.policy import AssistantError
from assistant.service import ensure_main_session, get_main_session
from db.base import Base, close_engine, get_db_session, init_engine
from db.models.project import Project
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from db.repository.user_repo import PgUserRepo
from project.workspace import session_counts
from session import session as sessions


@pytest.fixture(autouse=True)
async def assistant_database(tmp_path, monkeypatch):
    async def controlled_delivery(_result_id):
        return None
    # Unit cases exercise outbox acceptance/recovery explicitly. The full
    # roundtrip test restores this production hook to verify the fast path.
    monkeypatch.setattr("assistant.results.on_execution_result_committed", controlled_delivery)
    await close_engine()
    # Set only to a disposable PostgreSQL database/schema for independent
    # connection race tests. The default is a fresh SQLite file per test.
    url = os.environ.get("ASSISTANT_TEST_DATABASE_URL")
    if url:
        parsed = make_url(url)
        if parsed.host not in {"localhost", "127.0.0.1"} or not (parsed.database or "").startswith("openbox_assistant_test"):
            raise ValueError("Assistant tests require a local openbox_assistant_test* database")
    engine = init_engine(url or f"sqlite+aiosqlite:///{tmp_path / 'assistant.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield
    await close_engine()


async def accounts():
    suffix = uuid4().hex[:12]
    owner = await PgUserRepo().create(id=f"pa-owner-{suffix}", username=f"pa-owner-{suffix}",
                                     password_hash="unused")
    other = await PgUserRepo().create(id=f"pa-member-{suffix}", username=f"pa-member-{suffix}",
                                     password_hash="unused")
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(WorkspaceMember(workspace_id=owner["default_workspace_id"], user_id=other["id"],
                               role="member", status="active", created_at=now, updated_at=now))
    return owner["id"], other["id"], owner["default_workspace_id"]


async def test_read_does_not_create_and_two_devices_ensure_one_entry():
    owner, _, workspace = await accounts()
    assert await get_main_session(user_id=owner, workspace_id=workspace) is None
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(Session).where(Session.user_id == owner)) == 0
    first, second = await asyncio.gather(*(
        ensure_main_session(user_id=owner, workspace_id=workspace) for _ in range(2)
    ))
    assert first.id == second.id
    assert first.kind == first.agent == "assistant"
    assert first.visibility == "private" and first.memory_policy == "assistant_isolated"
    assert first.parent_id is None and first.sandbox_id is None
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(Session).where(Session.user_id == owner)) == 1
        assert await db.scalar(select(func.count()).select_from(Project).where(
            Project.workspace_id == workspace, Project.slug == "default")) == 1


async def test_read_audiences_cover_lists_counts_details_history_and_write_existence():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    public = await sessions.create_session(user_id=owner, workspace_id=workspace, title="Shared")
    private = await sessions.create_session(user_id=owner, workspace_id=workspace,
                                           visibility="private", title="Private task")
    actor = {"user_id": other, "workspace_id": workspace}
    assert [row["id"] for row in await routes.list_sessions(current_user=actor)] == [public.id]
    assert (await session_counts(workspace, user_id=other))[public.project_id] == 1
    assert (await session_counts(workspace, user_id=owner))[public.project_id] == 2
    for target in (main.id, private.id):
        for read in (routes.get_session, routes.get_messages, routes.get_history, routes._require_session_owned):
            with pytest.raises(HTTPException) as denied:
                await read(target, current_user=actor)
            assert denied.value.status_code == 404
    assert (await routes.get_session(public.id, current_user=actor))["id"] == public.id
    with pytest.raises(HTTPException) as readonly:
        await routes._require_session_owned(public.id, actor)
    assert readonly.value.status_code == 403
    assert (await sessions.get_session_in_workspace(private.id, workspace, user_id=owner)).id == private.id


async def test_assistant_kind_is_owner_only_even_if_audience_column_is_corrupt():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    async with get_db_session() as db:
        row = await db.get(Session, main.id)
        row.visibility = "workspace"
    assert await sessions.get_session_in_workspace(main.id, workspace, user_id=other) is None
    assert await sessions.list_sessions(user_id=other, workspace_id=workspace) == []


async def test_revoked_membership_cannot_read_or_ensure_even_own_private_entry():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=other, workspace_id=workspace)
    async with get_db_session() as db:
        member = await db.get(WorkspaceMember, (workspace, other))
        member.status = "removed"
    assert await sessions.get_session_in_workspace(main.id, workspace, user_id=other) is None
    assert await sessions.list_sessions(user_id=other, workspace_id=workspace) == []
    for operation in (get_main_session, ensure_main_session):
        with pytest.raises(AssistantError) as denied:
            await operation(user_id=other, workspace_id=workspace)
        assert denied.value.status == 403
    assert await get_main_session(user_id=owner, workspace_id=workspace) is None


async def test_partial_index_allows_one_entry_per_owner_and_workspace():
    owner, other, workspace = await accounts()
    first = await ensure_main_session(user_id=owner, workspace_id=workspace)
    second = await ensure_main_session(user_id=other, workspace_id=workspace)
    assert first.id != second.id
    with pytest.raises(IntegrityError):
        await sessions.create_session(user_id=owner, workspace_id=workspace, kind="assistant")


async def test_private_memory_policy_survives_child_and_cron_creation():
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    child = await sessions.create_session(user_id=owner, workspace_id=workspace, parent_id=main.id)
    cron = await sessions.create_session(user_id=owner, workspace_id=workspace, parent_id=child.id, kind="cron")
    for row in (child, cron):
        assert row.visibility == "private" and row.memory_policy == "assistant_isolated"
    ordinary = await sessions.create_session(user_id=owner, workspace_id=workspace)
    assert ordinary.visibility == "workspace" and ordinary.memory_policy == "standard"


async def test_session_settings_cannot_widen_audience_or_change_assistant_profile():
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    for change in ({"agent": "build"}, {"visibility": "workspace"}, {"memory_policy": "standard"},
                   {"kind": "normal"}, {"workspace_id": "elsewhere"}, {"project_id": "elsewhere"}):
        with pytest.raises(ValueError):
            await sessions.update_session(main.id, user_id=owner, **change)
    assert (await sessions.update_session(main.id, user_id=owner, title="My assistant")).title == "My assistant"
    with pytest.raises(HTTPException) as rejected:
        await routes.update_session(main.id, routes.UpdateSessionBody(agent="build"),
                                    {"user_id": owner, "workspace_id": workspace})
    assert rejected.value.status_code == 409


async def test_generic_delete_preserves_fixed_entry_and_its_future_task_links():
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    with pytest.raises(HTTPException) as rejected:
        await routes.delete_session(main.id, {"user_id": owner, "workspace_id": workspace})
    assert rejected.value.status_code == 409
    with pytest.raises(ValueError):
        await sessions.delete_session(main.id, user_id=owner, workspace_id=workspace)
    assert (await get_main_session(user_id=owner, workspace_id=workspace)).id == main.id


async def test_resource_and_billing_lists_hide_private_metadata_and_counts(monkeypatch):
    from types import SimpleNamespace
    from api import assets, billing
    from db.models.billing import UsageEvent
    from db.models.file_asset import FileAsset
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    public = await sessions.create_session(user_id=owner, workspace_id=workspace, title="Shared session")
    private = await sessions.create_session(user_id=owner, workspace_id=workspace,
                                           visibility="private", title="Private execution")
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        for session in (main, public, private):
            db.add(FileAsset(id=f"asset-{session.id}", user_id=owner, workspace_id=workspace,
                session_id=session.id, project_id=session.project_id, name=f"{session.id}.txt",
                oss_key=f"assets/{session.id}", mime="text/plain", size=10, status="ready",
                source="agent", transient=False, is_deleted=False, created_at=now))
            db.add(UsageEvent(id=f"usage-{session.id}", idempotency_key=f"usage-{session.id}",
                workspace_id=workspace, user_id=owner, session_id=session.id,
                session_title="Sensitive fallback title", model_id="test/model", kind="chat",
                tokens={}, total_tokens=1, credits=1, status="historical", pricing={}, created_at=now))
    monkeypatch.setattr(assets, "_oss_or_503", lambda: SimpleNamespace(presign_get=lambda key: key))
    for actor_id, expected in ((other, {public.id}), (owner, {main.id, public.id, private.id})):
        actor = {"user_id": actor_id, "workspace_id": workspace}
        files = await assets.list_assets(project="all", source="all", kind="all", q="", sort="created",
            limit=60, offset=0, current_user=actor, _workspace={"id": workspace})
        assert {item["sessionId"] for item in files["items"]} == expected
        assert files["total"] == len(expected)
        assert (await assets.asset_usage(actor, {"id": workspace}))["count"] == len(expected)
        usage = await billing.usage(page=1, page_size=20, workspace={"id": workspace},
            dates=billing.UsageDateRange(None, None), kind=None, user=actor)
        assert {item["session_id"] for item in usage["items"]} == expected
        assert usage["total"] == len(expected)
        assert (await billing.summary(workspace={"id": workspace}, dates=billing.UsageDateRange(None, None),
                                      kind=None, user=actor))["historical_count"] == len(expected)
    async with get_db_session() as db:
        with pytest.raises(HTTPException) as denied:
            await assets._owned_asset(db, f"asset-{private.id}", other, workspace)
        assert denied.value.status_code == 404
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
    async with get_db_session() as db:
        with pytest.raises(HTTPException) as revoked:
            await assets._owned_asset(db, f"asset-{private.id}", owner, workspace)
        assert revoked.value.status_code == 404
