"""First video: templates, welcome credits, the form endpoint, and the recovery hook."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from api.first_video import router
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from db.base import get_db_session
from db.models.billing import CreditLedger
from db.models.file_asset import FileAsset
from db.models.message import Message
from db.models.notification import Notification
from db.models.part import Part
from db.models.video_job import VideoJob
from db.repository.preference_repo import PgPreferenceRepo
from db.repository.user_repo import PgUserRepo
from light import first_video
from light.templates import first_video_templates


async def _user():
    uid = uuid4().hex
    return await PgUserRepo().create(id=uid, username="fv" + uid[:8], password_hash=None)


def _app(user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user["id"], "workspace_id": user["default_workspace_id"]}
    app.dependency_overrides[get_workspace] = lambda: {"id": user["default_workspace_id"], "role": "owner"}
    return app


def test_templates_cover_every_industry_and_template_and_never_name_the_shop():
    t = first_video_templates()
    assert t.industry_ids() == {"food", "beauty", "retail", "other"}
    assert t.template_ids() == {"signature", "ambience", "promo"}
    for industry in t.industry_ids():
        for template in t.template_ids():
            for with_image in (True, False):
                prompt = t.prompt(industry, template, with_image=with_image)
                assert prompt.endswith(t.negative)
                assert ("参考图" in prompt) is with_image
    assert t.prompt("unknown", "promo", with_image=False) == t.prompt("other", "promo", with_image=False)
    assert t.public()["eta_seconds"] == t.eta_seconds and len(t.public()["templates"]) == 3
    assert t.label("industry", "food") == "餐饮" and t.label("template", "nope") == "nope"


def test_notification_kinds_are_registered():
    from notifications import inbox
    from notifications.templates import render_template
    for kind in ("first_video_ready", "first_video_failed"):
        title, body = render_template(kind, "老王烧烤", "zh-CN")
        assert title and body
        assert inbox.category_for(kind) == "session"
    assert inbox.link_for("first_video_ready", workspace_id="w", session_id="s") == {
        "kind": "session", "workspaceId": "w", "sessionId": "s"}


async def test_signup_gift_is_posted_once_per_workspace():
    from billing.signup_gift import grant_signup_gift, grant_signup_gift_safely
    user = await _user()
    ws = user["default_workspace_id"]
    assert await grant_signup_gift(ws, user["id"]) == Decimal("4")
    assert await grant_signup_gift(ws, user["id"]) is None
    await grant_signup_gift_safely(ws, user["id"])
    await grant_signup_gift_safely(None, user["id"])
    async with get_db_session() as db:
        rows = (await db.scalars(select(CreditLedger).where(CreditLedger.workspace_id == ws))).all()
    assert [(r.kind, r.amount, r.reference_id) for r in rows] == [("signup_gift", Decimal("4"), user["id"])]


async def test_first_video_topup_only_under_enforce_and_only_once(monkeypatch):
    from billing.signup_gift import ensure_first_video_credits
    user = await _user()
    ws = user["default_workspace_id"]
    monkeypatch.setenv("BILLING_MODE", "shadow")
    assert await ensure_first_video_credits(ws, user["id"], Decimal("3.75")) is None
    monkeypatch.setenv("BILLING_MODE", "enforce")
    # The weekly free allowance (10) already covers a 15-second H3 clip.
    assert await ensure_first_video_credits(ws, user["id"], Decimal("3.75")) is None
    # A larger requirement is topped up to exactly the shortfall, once.
    assert await ensure_first_video_credits(ws, user["id"], Decimal("25")) == Decimal("15")
    assert await ensure_first_video_credits(ws, user["id"], Decimal("100")) is None
    async with get_db_session() as db:
        kinds = (await db.scalars(select(CreditLedger.kind).where(CreditLedger.workspace_id == ws)
                                  .order_by(CreditLedger.created_at))).all()
    assert kinds == ["allowance", "signup_gift"]


async def test_submit_runs_once_per_workspace_and_records_the_form(monkeypatch):
    user = await _user()
    started = []

    async def fake_drive(ctx, **kw):
        started.append((ctx.session_id, ctx.message_id, kw["key"], kw["reference"], kw["prompt"]))

    monkeypatch.setattr(first_video, "_drive", fake_drive)
    app = _app(user)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/first-video")).json()["status"] == "none"
        templates = (await client.get("/api/first-video/templates")).json()
        assert [t["id"] for t in templates["templates"]] == ["signature", "ambience", "promo"]
        first = (await client.post("/api/first-video", json={
            "industry": "food", "shop_name": " 老王  烧烤 ", "headline": "羊肉串", "template": "signature"})).json()
        assert first["status"] == "submitted" and first["session_id"] and first["eta_seconds"] > 0
        again = (await client.post("/api/first-video", json={"template": "promo"})).json()
        assert again["session_id"] == first["session_id"]
        assert (await client.post("/api/first-video", json={"template": "nope"})).status_code == 422
        assert (await client.get("/api/first-video")).json()["status"] == "submitted"
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(started) == 1
    session_id, message_id, key, reference, prompt = started[0]
    assert key == f"first_video:{user['default_workspace_id']}" and reference is None
    assert prompt.endswith(first_video_templates().negative) and "老王" not in prompt

    async with get_db_session() as db:
        messages = (await db.scalars(select(Message).where(Message.session_id == session_id)
                                     .order_by(Message.created_at))).all()
        parts = (await db.scalars(select(Part).where(Part.message_id == message_id))).all()
    assert [m.role for m in messages] == ["user", "assistant"] and messages[1].id == message_id
    assert len(parts) == 1 and parts[0].data["channel"] == "commentary"
    pref = (await PgPreferenceRepo().get(user["id"]))["extra"]["first_video"]
    assert pref["status"] == "submitted" and pref["shop_name"] == "老王 烧烤" and pref["headline"] == "羊肉串"
    assert pref["message_id"] == message_id and pref["template"] == "signature"
    from assistant import profile
    assert (await profile.load(user["id"])).business == "food"


async def test_offer_marks_new_accounts_and_reads_as_not_started():
    user = await _user()
    await first_video.offer(user["id"])
    pref = (await PgPreferenceRepo().get(user["id"]))["extra"]["first_video"]
    assert pref["status"] == "offered"
    assert (await first_video.state(user["id"], user["default_workspace_id"]))["status"] == "none"
    # A later offer never overwrites progress, and a legacy account simply has no record.
    await first_video._write_pref(user["id"], status="skipped")
    await first_video.offer(user["id"])
    assert (await first_video._pref(user["id"]))["status"] == "skipped"
    legacy = await _user()
    assert await first_video._pref(legacy["id"]) == {}


async def test_submit_refuses_a_reference_that_is_not_the_users_image():
    user = await _user()
    app = _app(user)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/first-video", json={"template": "promo", "asset_id": "missing"})
    assert response.status_code == 400 and response.json()["detail"]["code"] == "FIRST_VIDEO_INVALID"
    assert (await first_video.state(user["id"], user["default_workspace_id"]))["status"] == "none"


async def _settled_job(user, *, status="completed", key=None, shop_name=""):
    """A session with its reserved assistant message and a first-video job the driver never finished."""
    from session.session import create_assistant_message, create_session, create_user_message
    ws = user["default_workspace_id"]
    session = await create_session(model="m", agent="build", title="t", user_id=user["id"], workspace_id=ws)
    user_message = await create_user_message(session_id=session.id, text="form", user_id=user["id"])
    assistant = await create_assistant_message(session.id, user_message.id, user_id=user["id"])
    await first_video._write_pref(user["id"], status="running", workspace_id=ws, session_id=session.id,
                                  message_id=assistant.id, shop_name=shop_name, industry="food",
                                  template="signature", reference_asset_id=None, retried=False)
    now = datetime.now(timezone.utc)
    asset_id = "asset_" + uuid4().hex[:12]
    job_id = "video_" + uuid4().hex[:12]
    async with get_db_session() as db:
        db.add(FileAsset(id=asset_id, user_id=user["id"], workspace_id=ws, session_id=session.id,
                         name="first.mp4", oss_key=f"assets/{user['id']}/first.mp4", mime="video/mp4",
                         size=1234, status="ready" if status == "completed" else "failed", created_at=now))
        db.add(VideoJob(id=job_id, user_id=user["id"], session_id=session.id, kind="segment",
                        idempotency_key=key or first_video.job_key(ws), request_hash="h", status=status,
                        model="MiniMax-H3", output_asset_id=asset_id, request_data={}, result_data={},
                        error="provider said no" if status != "completed" else None,
                        attempt=1, created_at=now, updated_at=now, started_at=now, completed_at=now))
    return session, assistant, job_id, asset_id


async def test_recovery_delivers_a_finished_job_without_waking_the_agent():
    user = await _user()
    session, assistant, job_id, asset_id = await _settled_job(user)
    assert await first_video.sweep_unfinished() == 1
    pref = await first_video._pref(user["id"])
    assert pref["status"] == "ready" and pref["asset_id"] == asset_id
    async with get_db_session() as db:
        job = await db.get(VideoJob, job_id)
        parts = (await db.scalars(select(Part).where(Part.message_id == assistant.id))).all()
        message = await db.get(Message, assistant.id)
        notes = (await db.scalars(select(Notification).where(Notification.user_id == user["id"]))).all()
    assert job.attached_message_id == assistant.id
    assert sorted(p.type for p in parts) == ["file", "text"] and message.finish == "stop"
    assert [n.kind for n in notes] == ["first_video_ready"]
    state = await first_video.state(user["id"], user["default_workspace_id"])
    assert state["status"] == "ready" and state["asset_id"] == asset_id and state["session_id"] == session.id
    # A second sweep finds nothing left to do.
    assert await first_video.sweep_unfinished() == 0


async def test_recovery_marks_a_failed_job_and_tells_the_phone():
    user = await _user()
    session, assistant, job_id, _ = await _settled_job(user, status="failed")
    await first_video.sweep_unfinished()
    pref = await first_video._pref(user["id"])
    assert pref["status"] == "failed" and pref["error"]["message"] == "provider said no"
    async with get_db_session() as db:
        notes = (await db.scalars(select(Notification).where(Notification.user_id == user["id"]))).all()
        message = await db.get(Message, assistant.id)
    assert [n.kind for n in notes] == ["first_video_failed"] and message.finish == "stop"
    state = await first_video.state(user["id"], user["default_workspace_id"])
    assert state["status"] == "failed" and state["error"]["code"] == "GENERATION_FAILED"
    # The failed job is marked settled, so later sweeps skip it without reading the preference again.
    assert await first_video.sweep_unfinished() == 0
    async with get_db_session() as db:
        assert (await db.get(VideoJob, job_id)).result_data.get("first_video_settled") is True


def test_job_identity_by_key():
    class Job:
        kind = "segment"
        idempotency_key = "first_video:ws1"
    assert first_video.is_first_video_job(Job())
    Job.idempotency_key = "take-3"
    assert not first_video.is_first_video_job(Job())
    Job.idempotency_key, Job.kind = "first_video:ws1", "compose"
    assert not first_video.is_first_video_job(Job())
