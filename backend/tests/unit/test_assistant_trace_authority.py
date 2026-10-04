"""Current business audience, independent of delayed trajectory replicas."""
import pytest
from pydantic import ValidationError

from api.internal import TrajectoryAudienceQuery, trajectory_session_audience
from db.base import get_db_session
from db.models.session import Session
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from session.session import create_session
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


@pytest.fixture(autouse=True)
def admin_enabled(monkeypatch):
    monkeypatch.setenv("TRAJECTORY_ADMIN_ENABLED", "true")
    monkeypatch.delenv("TRAJECTORY_ADMIN_USER_IDS", raising=False)


def query(viewer, *sessions):
    return TrajectoryAudienceQuery(user_id=viewer, targets=[{
        "session_id": row.id, "user_id": row.user_id, "workspace_id": row.workspace_id,
    } for row in sessions])


async def principals():
    owner, admin, workspace = await accounts()
    async with get_db_session() as db:
        (await db.get(User, owner)).role = "admin"
        (await db.get(User, admin)).role = "admin"
    public = await create_session(user_id=owner, workspace_id=workspace)
    private = await create_session(user_id=owner, workspace_id=workspace, visibility="private")
    return owner, admin, workspace, public, private


async def test_private_owner_membership_and_original_scope_are_checked_without_changing_public_admin_access():
    owner, admin, workspace, public, private = await principals()
    assert (await trajectory_session_audience(query(admin, public, private)))["allowed"] == [public.id]
    assert (await trajectory_session_audience(query(owner, private)))["allowed"] == [private.id]
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
    assert (await trajectory_session_audience(query(owner, public, private)))["allowed"] == [public.id]
    for wrong in ({"user_id": admin}, {"workspace_id": "another-workspace"}, {"session_id": "unknown"}):
        body = query(admin, public).model_dump()
        body["targets"][0].update(wrong)
        assert not (await trajectory_session_audience(TrajectoryAudienceQuery(**body)))["allowed"]


@pytest.mark.parametrize("change", ["assistant_kind", "private_child", "private_grandchild", "cyclic_ancestry"])
async def test_a_shared_trace_never_overrides_private_descendant_or_assistant_audiences(change):
    owner, admin, workspace, public, _ = await principals()
    async with get_db_session() as db:
        if change == "assistant_kind":
            (await db.get(Session, public.id)).kind = "assistant"
    if change != "assistant_kind":
        child = await create_session(user_id=owner, workspace_id=workspace, parent_id=public.id,
                                     visibility="workspace" if change == "private_grandchild" else "private")
        if change == "private_grandchild":
            await create_session(user_id=owner, workspace_id=workspace, parent_id=child.id, visibility="private")
        if change == "cyclic_ancestry":
            async with get_db_session() as db:
                (await db.get(Session, public.id)).parent_id = child.id
    assert not (await trajectory_session_audience(query(admin, public)))["allowed"]
    assert (await trajectory_session_audience(query(owner, public)))["allowed"] == [public.id]


async def test_disabled_or_revoked_admin_and_unknown_sessions_do_not_get_a_replica_fallback(monkeypatch):
    owner, admin, _, public, _ = await principals()
    monkeypatch.setenv("TRAJECTORY_ADMIN_USER_IDS", owner)
    assert not (await trajectory_session_audience(query(admin, public)))["allowed"]
    monkeypatch.delenv("TRAJECTORY_ADMIN_USER_IDS")
    async with get_db_session() as db:
        (await db.get(User, admin)).role = "user"
    assert not (await trajectory_session_audience(query(admin, public)))["allowed"]
    async with get_db_session() as db:
        (await db.get(Session, public.id)).is_deleted = True
    assert not (await trajectory_session_audience(query(owner, public)))["allowed"]


def test_audience_batches_are_bounded_and_cannot_alias_two_owners_to_one_session():
    target = {"session_id": "session", "user_id": "owner", "workspace_id": "workspace"}
    for targets in ([target, {**target, "user_id": "other"}], [{**target, "session_id": str(i)} for i in range(201)]):
        with pytest.raises(ValidationError):
            TrajectoryAudienceQuery(user_id="admin", targets=targets)


@pytest.mark.parametrize("change", ["private", "owner", "workspace", "deleted", "shared"])
async def test_recorded_sources_keep_their_original_audience_after_reparenting(change):
    owner, admin, workspace, public, _ = await principals()
    child = await create_session(user_id=owner, workspace_id=workspace, parent_id=public.id)
    frozen = query(admin, public).model_dump()
    frozen["targets"][0]["sources"] = [{"session_id": child.id, "user_id": owner, "workspace_id": workspace}]
    async with get_db_session() as db:
        row = await db.get(Session, child.id)
        row.parent_id = None
        if change == "private": row.visibility = "private"
        if change == "owner": row.user_id = admin
        if change == "workspace":
            from sqlalchemy import select
            row.workspace_id = await db.scalar(select(Workspace.id).where(Workspace.owner_user_id == admin))
        if change == "deleted": row.is_deleted = True
    # Today's family no longer includes the source, but the recording still does.
    assert (await trajectory_session_audience(query(admin, public)))["allowed"] == [public.id]
    expected = [public.id] if change == "shared" else []
    assert (await trajectory_session_audience(TrajectoryAudienceQuery(**frozen)))["allowed"] == expected
    if change == "private":
        frozen["user_id"] = owner
        assert (await trajectory_session_audience(TrajectoryAudienceQuery(**frozen)))["allowed"] == [public.id]
