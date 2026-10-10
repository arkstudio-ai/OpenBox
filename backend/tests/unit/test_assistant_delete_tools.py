"""projects.delete, sessions.delete and tasks.delete on real SQL (docs/ASSISTANT_VOICE_FIX_PLAN.md 4).

Deletion follows the deletion boundary (backend/AGENTS.md): only the user's own
target, named on their explicit request, after they confirm the impact read
from the database on a card. 取消 does nothing; 确认 lets exactly that call
through once; a repeat never deletes twice.
"""
from dataclasses import replace
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from assistant.confirmations import CONFIRM_KIND, pending_cards
from assistant.runtime import ASSISTANT_PROMPT
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.cron import CronJob
from db.models.part import Part
from db.models.project import Project
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from models.message import ToolStatus
from question import question as q
from question.continuation import QuestionContinuationWorker
from session.session import create_session
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import (MAIN_MESSAGES, TOOLS, assistant, card, conversation, finish_turn,
                                                   human_turn, main_turn, tool_call, watch, watched_conversation)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "assistant-delete-tools-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)

    async def no_desktop(**_kwargs):
        raise AssertionError("deleting never acquires a cloud desktop")
    monkeypatch.setattr("sandbox.sandbox_manager.get_client_any", no_desktop)
    MAIN_MESSAGES.clear()


async def project_with(owner, workspace, name="贪吃蛇", chats=("界面", "关卡"), schedules=1):
    from project import workspace as projects
    project = await projects.create_project(owner, workspace, name)
    sessions = [await create_session(user_id=owner, workspace_id=workspace, project_id=project.id,
                                     model="test/model", title=title, visibility="private") for title in chats]
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        for index in range(schedules):
            db.add(CronJob(id=f"cron-{uuid4().hex}", user_id=owner, workspace_id=workspace, project_id=project.id,
                           name=f"每日检查 {index}", schedule={"kind": "every", "every_ms": 86400000},
                           task_prompt="检查一下", created_at=now, updated_at=now))
    return project, sessions


async def row(model, key):
    async with get_db_session() as db:
        return await db.get(model, key)


async def commands(owner, action):
    async with get_db_session() as db:
        return list((await db.scalars(select(AssistantCommand).where(AssistantCommand.actor_user_id == owner,
            AssistantCommand.action == action).order_by(AssistantCommand.created_at))).all())


async def main_cards(main_id):
    async with get_db_session() as db:
        return await db.scalar(select(func.count()).select_from(QuestionCheckpoint).where(
            QuestionCheckpoint.session_id == main_id))


# projects.delete -------------------------------------------------------------

async def test_project_delete_asks_first_cancel_does_nothing_and_confirm_deletes_once():
    owner, _, workspace, main = await assistant()
    project, chats = await project_with(owner, workspace)
    ctx, lease, human = await main_turn(owner, workspace, main, "把贪吃蛇项目删掉。")
    args = {"project_id": project.id, "source_message_ids": [human]}
    try:
        asked, part = await tool_call(ctx, "projects.delete", args)
        first = await card(asked["suspended"])
        assert first.questions[0]["question"] == (
            "删除项目「贪吃蛇」。\n影响：项目和其中 2 个会话会一起删除，无法在界面恢复；它的 1 个定时任务停用；"
            "项目文件夹之后会移到云电脑的回收站，一段时间内还能从那里找回文件。")
        assert first.questions[0]["header"] == "确认删除"
        assert [option["label"] for option in first.questions[0]["options"]] == ["确认", "取消"]
        assert first.continuation[CONFIRM_KIND]["action"] == "project_delete" and first.expires_at is not None
        async with get_db_session() as db:
            assert (await db.get(Part, part.id)).data["status"] == "waiting_input"
        await q.reply(first.id, [["取消"]], owner)
        retried, _ = await tool_call(ctx, "projects.delete", args)
        assert "suspended" in retried and retried["suspended"] != first.id
        assert (await row(Project, project.id)).is_deleted is False and await commands(owner, "project_delete") == []
        await q.reply(retried["suspended"], [["确认"]], owner)
        deleted, part = await tool_call(ctx, "projects.delete", args)
        assert part.status == ToolStatus.COMPLETED, deleted
        assert deleted["state"] == "deleted" and deleted["name"] == "贪吃蛇"
        assert deleted["conversations_deleted"] == 2 and deleted["schedules_stopped"] == 1
        # The same persisted call again returns its receipt; a new call finds nothing to delete.
        replay = await TOOLS["projects.delete"].execute(args, replace(ctx, part_id=part.id))
        assert json.loads(replay.output) == deleted
        again, _ = await tool_call(ctx, "projects.delete", args)
        assert again["error"] == "ASSISTANT_PROJECT_UNAVAILABLE"
    finally:
        await lease.release(session_status="idle")
    assert (await row(Project, project.id)).is_deleted is True
    async with get_db_session() as db:
        jobs = list((await db.scalars(select(CronJob).where(CronJob.project_id == project.id))).all())
    assert [(job.enabled, job.is_deleted) for job in jobs] == [(False, True)]
    [command] = await commands(owner, "project_delete")
    assert (command.state, command.target_type, command.target_id) == ("applied", "project", project.id)
    assert command.source_ref["confirmation_id"] == retried["suspended"]
    assert [ref["message_id"] for ref in command.source_ref["source_refs"]] == [human]
    assert "2 个会话" in command.source_ref["impact"]


async def test_a_busy_project_and_the_default_project_are_refused_before_any_card():
    owner, _, workspace, main = await assistant()
    project, chats = await project_with(owner, workspace, schedules=0)
    async with get_db_session() as db:
        (await db.get(Session, chats[0].id)).status = "busy"
    ctx, lease, human = await main_turn(owner, workspace, main, "把贪吃蛇和默认项目都删掉。")
    try:
        busy, _ = await tool_call(ctx, "projects.delete", {"project_id": project.id, "source_message_ids": [human]})
        assert busy["error"] == "ASSISTANT_PROJECT_BUSY" and "「界面」(busy)" in busy["message"]
        assert busy["message"].startswith("1 conversation(s)")
        default, _ = await tool_call(ctx, "projects.delete", {"project_id": main.project_id,
                                                              "source_message_ids": [human]})
        assert default["error"] == "ASSISTANT_PROJECT_PROTECTED"
    finally:
        await lease.release(session_status="idle")
    assert await main_cards(main.id) == 0
    assert (await row(Project, project.id)).is_deleted is False
    assert (await row(Project, main.project_id)).is_deleted is False


async def test_a_project_that_changed_after_the_card_is_shown_again():
    owner, _, workspace, main = await assistant()
    project, chats = await project_with(owner, workspace, chats=("界面",), schedules=0)
    ctx, lease, human = await main_turn(owner, workspace, main, "把贪吃蛇项目删掉。")
    args = {"project_id": project.id, "source_message_ids": [human]}
    try:
        asked, _ = await tool_call(ctx, "projects.delete", args)
        assert "其中 1 个会话" in (await card(asked["suspended"])).questions[0]["question"]
        await create_session(user_id=owner, workspace_id=workspace, project_id=project.id, model="test/model",
                             title="新会话", visibility="private")
        await q.reply(asked["suspended"], [["确认"]], owner)
        shown_again, _ = await tool_call(ctx, "projects.delete", args)
        assert shown_again["suspended"] != asked["suspended"]
        assert "其中 2 个会话" in (await card(shown_again["suspended"])).questions[0]["question"]
    finally:
        await lease.release(session_status="idle")
    assert (await row(Project, project.id)).is_deleted is False


# sessions.delete -------------------------------------------------------------

async def test_deleting_a_watched_conversation_confirmed_in_a_call_stops_following_it():
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "贪吃蛇关卡", visibility="private")
    await human_turn(session.id, owner, "加一个关卡", "加好了。")
    linked = await watch(owner, workspace, main, session)
    ctx, lease, human = await main_turn(owner, workspace, main, "把贪吃蛇关卡那个会话删了。")
    args = {"session_id": session.id, "source_message_ids": [human]}
    try:
        asked, part = await tool_call(ctx, "sessions.delete", args)
        await finish_turn(ctx, lease, "请确认删除。")
    finally:
        await lease.release(session_status="waiting_input")
    shown = await card(asked["suspended"])
    project = await row(Project, main.project_id)
    assert shown.questions[0]["question"] == (
        f"删除会话「贪吃蛇关卡」（项目「{project.name}」，2 条消息）。\n"
        "影响：会话和它的消息记录会从列表中删除，无法在界面恢复；我不再跟进它。")
    [listed] = await pending_cards(owner, workspace, main.id)
    assert listed["action"] == "session_delete" and listed["options"] == ["确认", "取消"]
    # The front desk read the card aloud and the user said 确认.
    await q.reply(listed["card_id"], [["确认"]], owner,
                  source_ref={"kind": "voice", "call_id": "call-1", "display_id": "spoken-1"})
    await QuestionContinuationWorker().tick()
    async with get_db_session() as db:
        resumed = await db.get(Part, part.id)
    assert resumed.data["metadata"]["confirmation"] == "confirmed"
    assert "call the same tool again" in resumed.data["output"]
    assert (await row(Session, session.id)).is_deleted is False
    ctx, lease, _ = await main_turn(owner, workspace, main, "继续。")
    try:
        deleted, part = await tool_call(ctx, "sessions.delete", args)
        assert part.status == ToolStatus.COMPLETED, deleted
        again, _ = await tool_call(ctx, "sessions.delete", args)
        assert again["error"] == "ASSISTANT_SESSION_UNAVAILABLE"
    finally:
        await lease.release(session_status="idle")
    assert deleted["state"] == "deleted" and deleted["task_archived"] is True
    assert (await row(Session, session.id)).is_deleted is True
    task = await row(AssistantTask, linked["task_id"])
    assert task.archived_at is not None and task.control_revision == linked["task_revision"] + 1
    [command] = await commands(owner, "session_delete")
    assert command.state == "applied" and command.source_ref["confirmation_id"] == shown.id


async def test_the_assistants_own_conversation_and_others_conversations_cannot_be_deleted():
    owner, other, workspace, main = await assistant()
    theirs = await create_session(user_id=other, workspace_id=workspace, model="test/model", title="成员的会话")
    mine = await conversation(owner, workspace, main, "共享会话", visibility="workspace")
    ctx, lease, human = await main_turn(owner, workspace, main, "把这些会话都删掉。")
    try:
        own, _ = await tool_call(ctx, "sessions.delete", {"session_id": main.id, "source_message_ids": [human]})
        assert own["error"] == "ASSISTANT_SESSION_PROTECTED"
        foreign, _ = await tool_call(ctx, "sessions.delete", {"session_id": theirs.id, "source_message_ids": [human]})
        assert foreign["error"] == "ASSISTANT_SESSION_UNAVAILABLE"
        forged, _ = await tool_call(ctx, "sessions.delete", {"session_id": mine.id,
                                                             "source_message_ids": [ctx.message_id]})
        assert forged["error"] == "ASSISTANT_SOURCE_UNVERIFIED"
        asked, _ = await tool_call(ctx, "sessions.delete", {"session_id": mine.id, "source_message_ids": [human]})
        assert "工作区成员也将看不到它" in (await card(asked["suspended"])).questions[0]["question"]
    finally:
        await lease.release(session_status="idle")
    assert await main_cards(main.id) == 1
    for session in (main, theirs, mine):
        assert (await row(Session, session.id)).is_deleted is False


# tasks.delete ----------------------------------------------------------------

async def test_deleting_a_running_task_cancels_it_and_stops_following_while_its_conversation_stays():
    from tests.unit.test_assistant_steering import running
    args, created, execution_lease, _ = await running()
    owner, workspace = args["user_id"], args["workspace_id"]
    main = await row(Session, args["main_id"])
    revision = (await row(AssistantTask, created["task_id"])).control_revision
    ctx, lease, human = await main_turn(owner, workspace, main, "停掉这个任务，不用再跟了。")
    request = {"task_id": created["task_id"], "expected_revision": revision, "source_message_ids": [human]}
    try:
        stale, _ = await tool_call(ctx, "tasks.delete", {**request, "expected_revision": revision + 5})
        assert stale["error"] == "ASSISTANT_TASK_REVISION"
        asked, _ = await tool_call(ctx, "tasks.delete", request)
        shown = await card(asked["suspended"])
        assert shown.questions[0]["header"] == "确认停止"
        assert shown.questions[0]["question"].startswith("停止并不再跟进任务「")
        assert shown.questions[0]["question"].endswith(
            "\n影响：正在做的会停下；已经完成的改动保留；会话仍留在项目里，之后还能打开。")
        assert (await row(AssistantTask, created["task_id"])).desired_state == "running"
        await q.reply(shown.id, [["确认"]], owner)
        deleted, part = await tool_call(ctx, "tasks.delete", request)
        assert part.status == ToolStatus.COMPLETED, deleted
        # The live run was asked to stop.
        assert (await row(AgentDriverState, created["execution_session_id"])).abort_requested_at is not None
        assert execution_lease.abort.is_set()
        again, _ = await tool_call(ctx, "tasks.delete", request)
    finally:
        await lease.release(session_status="idle")
        await execution_lease.release(session_status="idle")
    assert deleted["state"] == "archived" and deleted["canceled"] is True and deleted["desired_state"] == "canceled"
    assert again["state"] == "archived" and "nothing changed" in again["note"]
    task = await row(AssistantTask, created["task_id"])
    assert task.desired_state == "canceled" and task.archived_at is not None
    assert (await row(Session, created["execution_session_id"])).is_deleted is False
    [cancel] = await commands(owner, "task_cancel")
    assert cancel.receipt["command_id"] == deleted["cancel_command_id"]
    assert cancel.source_ref["via"]["action"] == "task_delete" and cancel.source_ref["via"]["source_message_ids"] == [human]
    [command] = await commands(owner, "task_delete")
    assert command.state == "applied" and command.source_ref["confirmation_id"] == shown.id
    assert await main_cards(main.id) == 1


async def test_deleting_an_idle_task_only_stops_following_it():
    owner, _, workspace, main = await assistant()
    session, linked = await watched_conversation(owner, workspace, main, visibility="private", title="周报")
    ctx, lease, human = await main_turn(owner, workspace, main, "周报那个任务不用再管了。")
    request = {"task_id": linked["task_id"], "expected_revision": linked["task_revision"],
               "source_message_ids": [human]}
    try:
        asked, _ = await tool_call(ctx, "tasks.delete", request)
        text = (await card(asked["suspended"])).questions[0]["question"]
        assert text.startswith("停止并不再跟进任务「周报」") and "不再向你汇报它的结果" in text
        await q.reply(asked["suspended"], [["取消"]], owner)
        declined, _ = await tool_call(ctx, "tasks.delete", request)
        assert declined["suspended"] != asked["suspended"]
        assert (await row(AssistantTask, linked["task_id"])).archived_at is None
        await q.reply(declined["suspended"], [["确认"]], owner)
        deleted, _ = await tool_call(ctx, "tasks.delete", request)
    finally:
        await lease.release(session_status="idle")
    assert deleted["state"] == "archived" and deleted["canceled"] is False
    task = await row(AssistantTask, linked["task_id"])
    assert task.archived_at is not None and task.desired_state == "running"
    assert (await row(Session, session.id)).is_deleted is False and await commands(owner, "task_cancel") == []


def test_prompt_deletes_only_on_explicit_request_after_a_card():
    prompt = " ".join(ASSISTANT_PROMPT.split())
    assert "You cannot delete conversations" not in prompt
    assert ("Delete a project, conversation or task only on the user's explicit request naming it "
            "(projects.delete, sessions.delete, tasks.delete). Each shows a confirmation card with the impact; "
            "in a call the front desk reads it and the user confirms by voice. Never delete on inference, from a "
            "summary, or because something looks unused; if the target is ambiguous, ask which one.") in prompt
