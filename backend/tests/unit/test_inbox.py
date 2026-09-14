"""Inbox helpers: link derivation, allow-listing, visibility, paging, retention."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from auth.mobile import now
from db.base import get_db_session
from db.models.notification import Notification
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from notifications import inbox


def test_link_for_maps_business_kinds_to_allow_listed_targets():
    assert inbox.link_for("task_completed", workspace_id="w", session_id="s") == {
        "kind": "session", "workspaceId": "w", "sessionId": "s"}
    assert inbox.link_for("cron_failed", workspace_id="w", action_id="job") == {
        "kind": "cron", "workspaceId": "w", "jobId": "job"}
    assert inbox.link_for("publish_done", workspace_id="w", action_id="pub") == {
        "kind": "auth_center", "workspaceId": "w", "jobId": "pub"}
    assert inbox.link_for("platform_auth_expired", workspace_id="w", action_id="acc") == {
        "kind": "auth_center", "workspaceId": "w"}
    assert inbox.link_for("skill_pending") == {"kind": "admin_skills"}
    assert inbox.link_for("skill_approved", workspace_id="w") == {"kind": "skills", "workspaceId": "w"}
    assert inbox.link_for("system_test") is None


@pytest.mark.parametrize("link", [
    {"kind": "url", "url": "https://localhost/x"},          # session producers never carry URLs
    {"kind": "session", "sessionId": "s"},                  # missing workspace
    {"kind": "topic", "slug": "Bad Slug"},
    {"kind": "open_app"},
    "not-a-dict",
])
def test_validate_link_rejects_unsafe_shapes(link):
    with pytest.raises(ValueError):
        inbox.validate_link(link)


def test_validate_link_allows_first_party_urls_only_on_known_hosts(monkeypatch):
    monkeypatch.setenv("ANNOUNCEMENT_LINK_HOSTS", "bossip.example")
    assert inbox.validate_link({"kind": "url", "url": "https://bossip.example/promo"}, first_party=True) == {
        "kind": "url", "url": "https://bossip.example/promo"}
    with pytest.raises(ValueError):
        inbox.validate_link({"kind": "url", "url": "https://evil.example/"}, first_party=True)
    with pytest.raises(ValueError):
        inbox.validate_link({"kind": "url", "url": "http://bossip.example/"}, first_party=True)
    assert inbox.validate_link({"kind": "session", "workspaceId": "w", "sessionId": "s", "panel": "desktop",
                                "control": 1, "extra": "dropped"}) == {
        "kind": "session", "workspaceId": "w", "sessionId": "s", "panel": "desktop", "control": True}


async def _scope():
    """Two workspaces; the user belongs only to the first."""
    user, other = uuid4().hex, uuid4().hex
    async with get_db_session() as db:
        for uid in (user, other):
            db.add(User(id=uid, username="u" + uid[:8], role="user", is_active=True, failed_login_count=0,
                        is_deleted=False, created_at=now(), updated_at=now()))
        mine, foreign = "ws" + uuid4().hex[:8], "ws" + uuid4().hex[:8]
        for wid, owner in ((mine, user), (foreign, other)):
            db.add(Workspace(id=wid, name=wid, owner_user_id=owner, kind="personal", is_deleted=False,
                             created_at=now(), updated_at=now()))
        await db.flush()
        db.add(WorkspaceMember(workspace_id=mine, user_id=user, role="owner", status="active",
                               created_at=now(), updated_at=now()))
        db.add(WorkspaceMember(workspace_id=foreign, user_id=other, role="owner", status="active",
                               created_at=now(), updated_at=now()))
        await db.commit()
    return user, mine, foreign


async def test_visibility_spans_memberships_and_hides_foreign_and_expired_rows():
    user, mine, foreign = await _scope()
    async with get_db_session() as db:
        visible_titles = ["own-in-mine", "own-account-level", "broadcast-in-mine"]
        await inbox.add_inbox(db, user_id=user, workspace_id=mine, kind="task_completed", title="own-in-mine")
        await inbox.add_inbox(db, user_id=user, workspace_id=None, kind="announcement", title="own-account-level",
                              category="notice")
        await inbox.add_inbox(db, user_id=None, workspace_id=mine, kind="desktop_login_reset", title="broadcast-in-mine")
        await inbox.add_inbox(db, user_id=user, workspace_id=foreign, kind="task_completed", title="own-in-foreign")
        await inbox.add_inbox(db, user_id=None, workspace_id=foreign, kind="desktop_login_reset", title="broadcast-foreign")
        await inbox.add_inbox(db, user_id=user, workspace_id=None, kind="announcement", title="expired",
                              category="notice", expires_at=now() - timedelta(seconds=1))
        await db.commit()
        rows, _ = await inbox.list_inbox(db, user, mine)
        assert sorted(r.title for r in rows) == sorted(visible_titles)
        assert await inbox.unread_counts(db, user, mine) == {"total": 3, "session": 1, "system": 1, "notice": 1}
        assert await inbox.mark_all_read(db, user, mine, "session") == 1
        assert (await inbox.unread_counts(db, user, mine))["total"] == 2


async def test_source_key_is_idempotent_and_resolution_marks_read():
    user, mine, _ = await _scope()
    async with get_db_session() as db:
        first = await inbox.add_inbox(db, user_id=user, workspace_id=mine, kind="input_required", title="q",
                                      source_key="question:1")
        again = await inbox.add_inbox(db, user_id=user, workspace_id=mine, kind="input_required", title="q2",
                                      source_key="question:1")
        assert again.id == first.id and again.title == "q"
        first_id = first.id
        await inbox.resolve_inbox(db, user, "question:1")
        await db.commit()
        row = await db.get(Notification, first_id)
        await db.refresh(row)
        assert row.resolved_at is not None and row.read_at is not None
        assert (await inbox.unread_counts(db, user, mine))["total"] == 0


async def test_cursor_paging_walks_every_row_once():
    user, mine, _ = await _scope()
    async with get_db_session() as db:
        for i in range(5):
            row = await inbox.add_inbox(db, user_id=user, workspace_id=mine, kind="task_completed", title=f"t{i}")
            row.created_at = now() - timedelta(minutes=5 - i)
        await db.commit()
        seen, cursor = [], None
        for _ in range(4):
            rows, cursor = await inbox.list_inbox(db, user, mine, cursor=cursor, limit=2)
            seen += [r.title for r in rows]
            if cursor is None:
                break
        assert seen == ["t4", "t3", "t2", "t1", "t0"]
        with pytest.raises(ValueError):
            await inbox.list_inbox(db, user, mine, cursor="nope")


async def test_sweep_applies_retention_by_category_and_expiry():
    user, mine, _ = await _scope()
    async with get_db_session() as db:
        old_session = await inbox.add_inbox(db, user_id=user, workspace_id=mine, kind="task_completed", title="old")
        old_session.created_at = now() - timedelta(days=91)
        keep_session = await inbox.add_inbox(db, user_id=user, workspace_id=mine, kind="task_completed", title="new")
        old_system = await inbox.add_inbox(db, user_id=user, workspace_id=mine, kind="publish_done", title="sys")
        old_system.created_at = now() - timedelta(days=400)
        long_expired = await inbox.add_inbox(db, user_id=user, workspace_id=None, kind="announcement", title="gone",
                                             category="notice", expires_at=now() - timedelta(days=31))
        ids = {name: row.id for name, row in (("old_session", old_session), ("keep_session", keep_session),
                                               ("old_system", old_system), ("long_expired", long_expired))}
        await db.commit()
        assert await inbox.sweep(db) == 2
        await db.commit()
        remaining = {r.id for r in (await db.scalars(
            select(Notification).where(Notification.user_id == user))).all()}
        assert remaining == {ids["keep_session"], ids["old_system"]}
