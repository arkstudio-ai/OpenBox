"""Business transitions commit the outbox exactly once and retire stale actions."""
import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from auth.mobile import mobile_transaction, now
from db.base import get_db_session
from db.models.cron import CronJob, CronRun
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.push import PushDelivery, PushMessage
from db.models.session import Session
from db.models.user import User
from db.models.workspace import WorkspaceMember
from notifications import events
from notifications.runtime import PushWorker
from question import question, runtime
from tests.integration.test_mobile_push_api import setup, login, bind, make_due_in_background  # noqa: F401


@pytest.fixture
async def task(setup):
    _, client, _, credentials, user, fake = setup
    await login(client, credentials)
    await bind(client, "ios")
    async with get_db_session() as db:
        # Business notifications remain available to ordinary users.
        (await db.get(User, user)).role = 'user'
        member = await db.scalar(select(WorkspaceMember).where(WorkspaceMember.user_id == user))
        project = Project(id=uuid4().hex, user_id=user, workspace_id=member.workspace_id,
                          name="Test", created_at=now(), updated_at=now())
        db.add(project)
        await db.flush()
        session = Session(id=uuid4().hex, user_id=user, workspace_id=member.workspace_id,
                          project_id=project.id, title="制作视频", kind="normal", status="idle",
                          created_at=now(), updated_at=now())
        db.add(session)
    return session, user, fake


async def messages(user):
    async with get_db_session() as db:
        return list((await db.scalars(select(PushMessage).where(PushMessage.user_id == user))).all())


async def ask_question(session, user):
    message_id, part_id = uuid4().hex, uuid4().hex
    async with get_db_session() as db:
        db.add(Message(id=message_id, session_id=session.id, user_id=user, role="assistant",
                       finish="waiting_input", created_at=now()))
        await db.flush()
        db.add(Part(id=part_id, session_id=session.id, message_id=message_id, user_id=user, type="tool",
                    data={"id": part_id, "type": "tool", "tool": "question", "status": "running"}, created_at=now()))
    await question.ask(session.id, [question.Question(question="选哪个？",
        options=[question.QuestionOption(label="一个")])],
        {"messageID": message_id, "callID": part_id}, user_id=user)


@pytest.mark.parametrize("outcome", ["completed", "failed", "idle", "cancelled", "waiting", "child", "cron"])
async def test_only_explicit_top_level_terminal_results_notify(task, outcome):
    session, user, fake = task
    if outcome in {"child", "cron"}:
        async with get_db_session() as db:
            saved = await db.get(Session, session.id)
            if outcome == "child":
                saved.parent_id = session.id
            else:
                saved.kind = "cron"
    ticket = await runtime.start_run(session.id, user)
    if outcome == "cancelled":
        await runtime.cancel_session(session.id, user)
    if outcome == "waiting":
        context = runtime.current_run.set(ticket)
        try:
            with pytest.raises(question.QuestionSuspended):
                await ask_question(session, user)
        finally:
            runtime.current_run.reset(context)
    await runtime.finish_run(ticket, completed=outcome != "idle", failed=outcome == "failed")
    await runtime.finish_run(ticket, completed=True)  # Duplicate finalization.
    await make_due_in_background(user)
    await PushWorker(fake).tick()
    expected = {"completed": "task_completed", "failed": "task_failed", "waiting": "input_required"}.get(outcome)
    assert [p[3]["type"] for p in fake.sent] == ([expected] if expected else [])
    assert len(await messages(user)) == (1 if expected else 0)


async def test_question_resolution_cancels_waiting_notification_in_same_transaction(task):
    session, user, fake = task
    with pytest.raises(question.QuestionSuspended) as suspended:
        await ask_question(session, user)
    notes = await messages(user)
    assert len(notes) == 1 and notes[0].payload["type"] == "input_required"
    await question.reply(suspended.value.request_id, [["一个"]], user_id=user)
    await make_due_in_background(user)
    await PushWorker(fake).tick()
    assert fake.sent == []
    async with get_db_session() as db:
        row = await db.scalar(select(PushDelivery).where(PushDelivery.message_id == notes[0].id))
        assert (row.status, row.error) == ("cancelled", "action_resolved")


async def test_new_turn_invalidates_an_unsent_completion(task):
    session, user, fake = task
    ticket = await runtime.start_run(session.id, user)
    await runtime.finish_run(ticket, completed=True)
    async with runtime.transaction(session.id, user) as (db, _, execution):
        await runtime.invalidate_locked(db, execution)
    await make_due_in_background(user)
    await PushWorker(fake).tick()
    assert fake.sent == []


async def test_permission_ask_and_reply_notify_only_while_actually_blocked(task, monkeypatch):
    from permission import permission
    session, user, fake = task
    monkeypatch.setattr(permission, "_get_redis_client", lambda: None)
    ticket = await runtime.start_run(session.id, user)
    context = runtime.current_run.set(ticket)
    waiter = asyncio.create_task(permission.ask(session.id, "edit", ["secret.txt"], user_id=user))
    runtime.current_run.reset(context)
    try:
        for _ in range(100):
            if await messages(user):
                break
            await asyncio.sleep(0.01)
        notes = await messages(user)
        assert len(notes) == 1 and notes[0].payload["type"] == "approval_required"
        await permission.reply(notes[0].payload["actionId"], "once", user_id=user)
        await asyncio.wait_for(waiter, 1)
        await make_due_in_background(user)
        await PushWorker(fake).tick()
        assert fake.sent == []
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await runtime.finish_run(ticket)


async def create_cron(session, user, *, schedule=None, notify=True, status="ok", summary="有新结果"):
    job_id, run_id = uuid4().hex, uuid4().hex
    async with get_db_session() as db:
        db.add(CronJob(id=job_id, user_id=user, workspace_id=session.workspace_id, project_id=session.project_id,
            session_id=session.id, name="定时任务", schedule=schedule or {"kind": "at", "at": now().isoformat()},
            task_prompt="Do work", delivery={"notifications_enabled": notify}, enabled=True,
            delete_after_run=False, max_retries=3, next_run_at=now(), created_at=now(), updated_at=now()))
        db.add(CronRun(id=run_id, job_id=job_id, user_id=user, session_id=session.id,
            project_id=session.project_id, status=status, summary_text=summary,
            started_at=now(), ended_at=now()))
    return job_id, {"status": status, "run_id": run_id, "summary_text": summary}


@pytest.mark.parametrize("summary,notify,expected", [("新的结果", True, 1), ("NO_REPLY", True, 0),
                                                     ("", True, 0), ("新的结果", False, 0)])
async def test_cron_result_is_meaningful_enabled_and_deduplicated(task, summary, notify, expected):
    from cron.timer import TimerState, _apply_job_result
    session, user, fake = task
    job_id, result = await create_cron(session, user, notify=notify, summary=summary)
    await _apply_job_result(TimerState(), job_id, result)
    await _apply_job_result(TimerState(), job_id, result)
    await make_due_in_background(user)
    await PushWorker(fake).tick()
    assert len(fake.sent) == expected
    if expected:
        assert fake.sent[0][3]["type"] == "cron_completed"


async def test_cron_retry_is_quiet_and_only_final_failure_notifies(task):
    from cron.timer import TimerState, _apply_job_result
    session, user, fake = task
    job_id, result = await create_cron(session, user, status="error", summary="")
    result["error"] = "network timeout"
    await _apply_job_result(TimerState(), job_id, result)
    assert await messages(user) == []
    async with get_db_session() as db:
        job = await db.get(CronJob, job_id)
        job.consecutive_errors = 3
    await _apply_job_result(TimerState(), job_id, result)
    await make_due_in_background(user)
    await PushWorker(fake).tick()
    assert [p[3]["type"] for p in fake.sent] == ["cron_failed"]


async def test_publish_submission_and_expiry_are_quiet_confirmed_result_notifies_once(task):
    from db.models.publish_job import PublishJob
    from platforms.service import handle_douyin_event
    session, user, fake = task
    async with get_db_session() as db:
        job = PublishJob(id=uuid4().hex, workspace_id=session.workspace_id, user_id=user,
            platform="douyin", file_asset_id="test-asset", title="视频", share_id=uuid4().hex,
            status="pending", expires_at=now() + timedelta(hours=1), created_at=now(), updated_at=now())
        db.add(job)
        await events.publish_result(db, job)
    assert await messages(user) == []
    payload = {"event": "create_video", "content": {"share_id": job.share_id, "item_id": "platform-item"}}
    assert await handle_douyin_event(payload)
    assert not await handle_douyin_event(payload)
    await make_due_in_background(user)
    await PushWorker(fake).tick()
    assert [p[3]["type"] for p in fake.sent] == ["publish_done"]


async def test_auth_maintenance_is_quiet_but_an_affected_active_task_notifies(task):
    from db.models.platform_account import PlatformAccount
    session, user, fake = task
    ticket = await runtime.start_run(session.id, user)
    async with get_db_session() as db:
        account = PlatformAccount(id=uuid4().hex, workspace_id=session.workspace_id, bound_by_user_id=user,
            platform="douyin", auth_kind="oauth", external_id="platform-user", scopes="", status="expired",
            renew_count=0, bound_at=now(), created_at=now(), updated_at=now())
        db.add(account)
        await db.flush()
        await events.auth_blocked(db, account)
    assert await messages(user) == []
    async with get_db_session() as db:
        account = await db.get(PlatformAccount, account.id)
        await events.auth_blocked(db, account, session_id=session.id, user_id=user)
        await events.auth_blocked(db, account, session_id=session.id, user_id=user)
    assert len(await messages(user)) == 1
    # Reauthorization before dispatch removes the need to interrupt the user.
    async with get_db_session() as db:
        (await db.get(PlatformAccount, account.id)).status = "bound"
    await make_due_in_background(user)
    await PushWorker(fake).tick()
    assert fake.sent == []
    await runtime.finish_run(ticket)


async def test_business_transaction_rollback_does_not_leave_an_outbox_entry(task):
    session, user, _ = task
    with pytest.raises(RuntimeError):
        async with get_db_session() as db:
            await events.emit(db, user_id=user, workspace_id=session.workspace_id, session_id=session.id,
                              kind="task_completed", event_key="rollback", name="Result")
            raise RuntimeError("Business transaction failed")
    assert await messages(user) == []
