"""「任务完成」 must not reach the owner before the Light cut does (docs/LIGHT_FOOTAGE_EDIT.md)."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sqlalchemy import select

from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.file_asset import FileAsset
from db.models.notification import Notification
from db.models.session import Session
from db.models.video_job import VideoJob
from light.production_form import initial_form
from models.message import FilePart, FileRelation
from notifications import events
from session import session as session_mod
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _job(user_id, session_id, status):
    now = datetime.now(timezone.utc)
    job_id = f"video_{uuid4().hex[:16]}"
    async with get_db_session() as db:
        db.add(VideoJob(id=job_id, user_id=user_id, session_id=session_id, kind="render",
                        idempotency_key=job_id, request_hash="h", status=status,
                        request_data={}, result_data={}, created_at=now, updated_at=now))
        await db.commit()


def _session(workspace, *, light_entry):
    return SimpleNamespace(id=f"session_{uuid4().hex[:16]}", parent_id=None, kind="chat",
                           workspace_id=workspace, title="你拍我剪", light_entry=light_entry)


async def _finish(monkeypatch, session, owner, *, failed=False):
    seen = []

    async def emit(db, **kwargs):
        seen.append(kwargs["kind"])

    monkeypatch.setattr(events, "emit", emit)
    ticket = SimpleNamespace(user_id=owner, run_id="run-1", generation=1, session_id=session.id)
    async with get_db_session() as db:
        await events.task_finished(db, session, ticket, failed=failed)
    return seen


async def test_light_turn_with_a_cut_still_rendering_sends_no_task_completed(monkeypatch):
    owner, _, workspace = await accounts()
    session = _session(workspace, light_entry="footage_edit")
    await _job(owner, session.id, "in_progress")
    assert await _finish(monkeypatch, session, owner) == []


async def test_light_turn_after_the_cut_settled_sends_task_completed(monkeypatch):
    owner, _, workspace = await accounts()
    session = _session(workspace, light_entry="footage_edit")
    await _job(owner, session.id, "completed")
    assert await _finish(monkeypatch, session, owner) == ["task_completed"]


async def test_menu_prepare_requests_review_and_the_delivered_image_only_completes_once():
    owner, _, workspace = await accounts()
    stamp = datetime.now(timezone.utc)
    created = await session_mod.create_session(
        user_id=owner, workspace_id=workspace, title="做菜单", light_entry="menu",
        light_production={**initial_form("menu", "测试餐厅", ""),
                          "page": "review", "notes": "周末特惠菜单", "agent_revision": 2,
                          "run": {"id": "prep", "action": "prepare", "revision": 1}},
    )
    ticket = SimpleNamespace(user_id=owner, run_id="run-1", generation=1)
    async with get_db_session() as db:
        row = await db.get(Session, created.id)
        db.add(AgentInboxItem(
            id="prep", user_id=owner, project_id=row.project_id, session_id=row.id,
            request_digest="0" * 64, delivery="followup", target="next-turn", prompt="prepare", attachments=[],
            state="settled", run_id=ticket.run_id, generation=1, turn_id="turn-1", step_id="step-1",
            claim_token="claim", claim_owner="test", outcome="succeeded", accepted_at=stamp, claimed_at=stamp,
            created_at=stamp, updated_at=stamp, settled_at=stamp,
        ))
        await events.task_finished(db, row, ticket)
        notices = (await db.scalars(select(Notification).where(Notification.user_id == owner))).all()
        assert [(n.kind, n.body) for n in notices] == [
            ("approval_required", "《周末特惠菜单》需要你的确认。"),
        ]
        assert notices[0].link["production"] is True
        row.light_production = {**row.light_production, "run": {"id": "prep", "action": "render"}}
        db.add(FileAsset(id="menu-asset", user_id=owner, workspace_id=workspace, session_id=row.id,
                         name="menu.png", mime="image/png", oss_key="menu.png", status="ready",
                         source="agent", created_at=stamp))
        await events.task_finished(db, row, ticket)
        assert not await db.scalar(select(Notification.id).where(
            Notification.user_id == owner, Notification.kind == "task_completed"))

    user = await session_mod.create_user_message(created.id, "开始生成", user_id=owner)
    assistant = await session_mod.create_assistant_message(created.id, user.id, user_id=owner)
    await session_mod.save_part(FilePart(
        session_id=created.id, message_id=assistant.id, path="/workspace/menu.png",
        mime_type="image/png", asset_id="menu-asset", relation=FileRelation(role="final"),
    ), is_new=True, user_id=owner)
    async with get_db_session() as db:
        row = await db.get(Session, created.id)
        await events.task_finished(db, row, ticket)
        await events.task_finished(db, row, SimpleNamespace(user_id=owner, run_id="run-2", generation=2))
        notices = (await db.scalars(select(Notification).where(
            Notification.user_id == owner, Notification.kind == "task_completed"))).all()
        assert [(n.source_key, n.body) for n in notices] == [
            (f"production:{row.id}:output:menu-asset", "《周末特惠菜单》已完成，查看结果。"),
        ]


async def test_a_failed_light_turn_still_reports_the_failure(monkeypatch):
    owner, _, workspace = await accounts()
    session = _session(workspace, light_entry="footage_edit")
    await _job(owner, session.id, "in_progress")
    assert await _finish(monkeypatch, session, owner, failed=True) == ["task_failed"]


async def test_an_ordinary_conversation_waits_for_its_film_too(monkeypatch):
    owner, _, workspace = await accounts()
    session = _session(workspace, light_entry=None)
    await _job(owner, session.id, "in_progress")
    assert await _finish(monkeypatch, session, owner) == []
