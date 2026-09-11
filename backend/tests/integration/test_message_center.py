"""Message centre end to end: business events, announcements, topics, push."""
import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from bus import bus
from bus.events import INBOX_UPDATED
from db.base import get_db_session
from db.models.notification import Announcement, Notification
from db.models.push import PushDelivery, PushMessage
from notifications import announcements, events
from notifications.runtime import PushWorker
from question import question, runtime
from tests.integration.test_mobile_push_api import setup, login, bind, make_due_in_background  # noqa: F401
from tests.integration.test_mobile_notification_events import task, ask_question, messages  # noqa: F401


async def register(setup):
    """A second, ordinary user on the same app."""
    web = setup[0]
    client = httpx.AsyncClient(transport=web._transport, base_url="http://test")
    credentials = {"username": "user-" + uuid4().hex[:12], "password": "password123"}
    response = await client.post("/api/auth/register", json=credentials)
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
    return client, response.json()["user"]["id"]


async def test_business_event_lands_in_inbox_and_push_points_back(setup, task):
    web, _, _, _, user, fake = setup
    session, _, _ = task
    ticket = await runtime.start_run(session.id, user)
    await runtime.finish_run(ticket, completed=True)
    listing = (await web.get("/api/inbox")).json()
    assert listing["unread"] == {"total": 1, "session": 1, "system": 0, "notice": 0}
    item = listing["items"][0]
    assert item["category"] == "session" and item["kind"] == "task_completed"
    assert item["link"] == {"kind": "session", "workspaceId": session.workspace_id, "sessionId": session.id}
    push = (await messages(user))[0]
    assert push.payload["notificationId"] == item["id"]
    assert (await web.post(f"/api/inbox/{item['id']}/read")).json()["readAt"]
    assert (await web.get("/api/inbox/unread")).json()["total"] == 0
    # Second read is idempotent; someone else's id is not found.
    assert (await web.post(f"/api/inbox/{item['id']}/read")).status_code == 200
    assert (await web.post("/api/inbox/ntf_missing/read")).status_code == 404


async def test_question_reply_resolves_the_inbox_row(setup, task):
    web, _, _, _, user, fake = setup
    session, _, _ = task
    with pytest.raises(question.QuestionSuspended) as suspended:
        await ask_question(session, user)
    before = (await web.get("/api/inbox?category=session")).json()
    assert before["unread"]["session"] == 1 and before["items"][0]["kind"] == "input_required"
    await question.reply(suspended.value.request_id, [["一个"]], user_id=user)
    after = (await web.get("/api/inbox?category=session")).json()
    assert after["unread"]["session"] == 0
    assert after["items"][0]["resolvedAt"] and after["items"][0]["readAt"]


async def test_inbox_updated_event_fires_after_commit(setup, task):
    _, _, _, _, user, _ = setup
    session, _, _ = task
    seen = []
    unsubscribe = bus.subscribe(INBOX_UPDATED, lambda event: seen.append(event))
    try:
        ticket = await runtime.start_run(session.id, user)
        await runtime.finish_run(ticket, completed=True)
        await asyncio.sleep(0)
    finally:
        unsubscribe()
    assert [e["data"]["userId"] for e in seen] == [user]


async def test_announcement_fanout_is_idempotent_and_revocable(setup):
    web, _, _, _, admin, _ = setup
    other, other_id = await register(setup)
    created = await web.post("/api/admin/messages/announcements", json={
        "title": "版本更新", "body": "新版本已上线", "link": {"kind": "topic", "slug": "release-2026-09"},
        "audience": {"kind": "all"}})
    assert created.status_code == 201, created.text
    ann = created.json()
    assert ann["status"] == "draft" and ann["recipientCount"] == 2
    assert (await other.post(f"/api/admin/messages/announcements/{ann['id']}/publish")).status_code == 403
    published = await web.post(f"/api/admin/messages/announcements/{ann['id']}/publish")
    assert published.status_code == 200 and published.json()["status"] == "published"
    # The publish endpoint fans out in the background; re-running is a no-op.
    first = await announcements.fan_out(ann["id"])
    assert first in (0, 2) and await announcements.fan_out(ann["id"]) == 0
    async with get_db_session() as db:
        rows = list((await db.scalars(select(Notification).where(Notification.announcement_id == ann["id"]))).all())
        assert sorted(r.user_id for r in rows) == sorted([admin, other_id])
        assert all(r.category == "notice" and r.link == {"kind": "topic", "slug": "release-2026-09"} for r in rows)
    for client in (web, other):
        listing = (await client.get("/api/inbox?category=notice")).json()
        assert [i["title"] for i in listing["items"]] == ["版本更新"]
        assert listing["unread"]["notice"] == 1
    assert (await web.put(f"/api/admin/messages/announcements/{ann['id']}", json={"title": "x"})).status_code == 409
    revoked = await web.post(f"/api/admin/messages/announcements/{ann['id']}/revoke")
    assert revoked.status_code == 200 and revoked.json()["hidden"] == 2
    for client in (web, other):
        assert (await client.get("/api/inbox?category=notice")).json()["items"] == []
    overview = (await web.get("/api/admin/messages/announcements")).json()["items"]
    assert overview[0]["status"] == "revoked" and overview[0]["fanoutCount"] == 2
    await other.aclose()


async def test_targeted_audience_and_link_allow_list(setup):
    web, _, _, _, admin, _ = setup
    other, other_id = await register(setup)
    bad = await web.post("/api/admin/messages/announcements", json={
        "title": "钓鱼", "link": {"kind": "url", "url": "https://evil.example/"}, "audience": {"kind": "all"}})
    assert bad.status_code == 422
    assert (await web.post("/api/admin/messages/announcements", json={
        "title": "x", "audience": {"kind": "users", "ids": []}})).status_code == 422
    created = await web.post("/api/admin/messages/announcements", json={
        "title": "仅你可见", "link": {"kind": "url", "url": "https://localhost/promo"},
        "audience": {"kind": "users", "ids": [other_id]}})
    assert created.status_code == 201, created.text
    assert created.json()["recipientCount"] == 1
    await web.post(f"/api/admin/messages/announcements/{created.json()['id']}/publish")
    await announcements.fan_out(created.json()["id"])
    assert (await web.get("/api/inbox?category=notice")).json()["items"] == []
    items = (await other.get("/api/inbox?category=notice")).json()["items"]
    assert [i["link"] for i in items] == [{"kind": "url", "url": "https://localhost/promo"}]
    await other.aclose()


async def test_announcement_push_uses_the_outbox_and_respects_revocation(setup):
    web, ios, _, credentials, admin, fake = setup
    await login(ios, credentials)
    await bind(ios, "ios")
    first = (await web.post("/api/admin/messages/announcements", json={
        "title": "推送公告", "body": "看看", "push": True, "audience": {"kind": "role", "role": "admin"}})).json()
    await web.post(f"/api/admin/messages/announcements/{first['id']}/publish")
    await announcements.fan_out(first["id"])
    async with get_db_session() as db:
        push = await db.scalar(select(PushMessage).where(PushMessage.event_key == f"announcement:{first['id']}"))
        row = await db.scalar(select(Notification).where(Notification.announcement_id == first["id"]))
        assert push.payload["type"] == "notice" and push.payload["notificationId"] == row.id
        assert push.payload["guard"] == {"kind": "announcement", "id": first["id"]}
    await make_due_in_background(admin)
    await PushWorker(fake).tick()
    assert [p[3]["type"] for p in fake.sent] == ["notice"]

    second = (await web.post("/api/admin/messages/announcements", json={
        "title": "撤回公告", "push": True, "audience": {"kind": "role", "role": "admin"}})).json()
    await web.post(f"/api/admin/messages/announcements/{second['id']}/publish")
    await announcements.fan_out(second["id"])
    await web.post(f"/api/admin/messages/announcements/{second['id']}/revoke")
    await make_due_in_background(admin)
    await PushWorker(fake).tick()
    assert len(fake.sent) == 1
    async with get_db_session() as db:
        message = await db.scalar(select(PushMessage).where(PushMessage.event_key == f"announcement:{second['id']}"))
        delivery = await db.scalar(select(PushDelivery).where(PushDelivery.message_id == message.id))
        assert delivery.status == "cancelled"

    preview = await web.post(f"/api/admin/messages/announcements/{first['id']}/preview")
    assert preview.status_code == 202 and preview.json()["title"].startswith("预览 · ")


async def test_scheduled_announcement_publishes_when_due(setup):
    web, _, _, _, admin, _ = setup
    later = (await web.post("/api/admin/messages/announcements", json={
        "title": "定时", "audience": {"kind": "all"},
        "publishAt": (announcements.now() + timedelta(hours=1)).isoformat()})).json()
    scheduled = await web.post(f"/api/admin/messages/announcements/{later['id']}/publish")
    assert scheduled.json()["status"] == "scheduled"
    janitor = announcements.InboxJanitor()
    await janitor.tick()
    assert (await web.get("/api/inbox?category=notice")).json()["items"] == []
    async with get_db_session() as db:
        (await db.get(Announcement, later["id"])).publish_at = announcements.now() - timedelta(seconds=1)
        await db.commit()
    await janitor.tick()
    listing = (await web.get("/api/inbox?category=notice")).json()
    assert [i["title"] for i in listing["items"]] == ["定时"]
    assert (await web.get(f"/api/admin/messages/announcements/{later['id']}")).json()["status"] == "published"


async def test_topics_are_public_only_once_published(setup):
    web, _, _, _, _, _ = setup
    anonymous = httpx.AsyncClient(transport=web._transport, base_url="http://test")
    body = {"slug": "release-2026-09", "title": "九月更新", "contentMd": "# 新功能\n消息中心上线。",
            "ctaLabel": "去看看", "ctaLink": {"kind": "skills", "workspaceId": "ws"}}
    created = await web.post("/api/admin/messages/topics", json=body)
    assert created.status_code == 201, created.text
    topic = created.json()
    assert (await anonymous.get("/api/topics/release-2026-09")).status_code == 404
    assert (await web.post("/api/admin/messages/topics", json=body)).status_code == 409
    assert (await web.post("/api/admin/messages/topics", json={**body, "slug": "Bad_Slug"})).status_code == 422
    assert (await web.post("/api/admin/messages/topics", json={**body, "slug": "b", "ctaLink": None})).status_code == 422
    assert (await web.post(f"/api/admin/messages/topics/{topic['id']}/publish")).json()["status"] == "published"
    public = await anonymous.get("/api/topics/release-2026-09")
    assert public.status_code == 200
    assert public.json()["contentMd"].startswith("# 新功能") and public.json()["ctaLabel"] == "去看看"
    assert "status" not in public.json() and "createdBy" not in public.json()
    assert (await web.put(f"/api/admin/messages/topics/{topic['id']}", json={**body, "slug": "renamed"})).status_code == 409
    assert (await web.put(f"/api/admin/messages/topics/{topic['id']}", json={**body, "title": "改标题"})).status_code == 200
    await web.post(f"/api/admin/messages/topics/{topic['id']}/unpublish")
    assert (await anonymous.get("/api/topics/release-2026-09")).status_code == 404
    assert (await anonymous.get("/api/topics/Bad%20Slug")).status_code == 404
    await anonymous.aclose()


async def test_legacy_strip_endpoint_still_serves_workspace_rows(setup):
    web, _, _, _, admin, _ = setup
    from platforms.service import add_notification
    workspace = (await web.get("/api/auth/me")).json()
    async with get_db_session() as db:
        member_workspace = await db.scalar(
            select(__import__("db.models.workspace", fromlist=["WorkspaceMember"]).WorkspaceMember.workspace_id)
            .where(__import__("db.models.workspace", fromlist=["WorkspaceMember"]).WorkspaceMember.user_id == admin))
        await add_notification(db, workspace_id=member_workspace, user_id=None, kind="desktop_login_reset",
                               title="云电脑已更换", body="请重新登录")
        await db.commit()
    legacy = (await web.get("/api/notifications?unread=true")).json()
    assert legacy["unread"] == 1 and legacy["items"][0]["link"] == {"kind": "auth_center", "workspaceId": member_workspace}
    inbox = (await web.get("/api/inbox?category=system")).json()
    assert [i["title"] for i in inbox["items"]] == ["云电脑已更换"]
    assert (await web.post("/api/inbox/read-all?category=system")).json()["updated"] == 1
    assert (await web.get("/api/notifications?unread=true")).json()["unread"] == 0
