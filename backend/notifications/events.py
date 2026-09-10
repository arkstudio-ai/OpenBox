"""Explicit business signals and fixed bilingual lock-screen templates.

No stream text, arbitrary tool output, credentials or provider errors enter a
push. Call transactional helpers from the transaction that commits the result.
"""
from sqlalchemy import select, update

from auth.mobile import lock_mutation, now, utc
from db.models.push import PushDelivery, PushDevice, PushMessage
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session
from notifications.store import can_receive, enqueue_notification

TEMPLATES = {
    "task_completed": ("任务已完成", "《{name}》已完成，点击查看结果。", "Task completed", '“{name}” is complete. Tap to view the result.'),
    "task_failed": ("任务未能完成", "《{name}》已停止执行，点击查看并处理。", "Task could not finish", '“{name}” has stopped. Tap to review and continue.'),
    "input_required": ("任务需要你的回答", "《{name}》正在等待你的回答，点击继续。", "Your answer is needed", '“{name}” is waiting for your answer. Tap to continue.'),
    "approval_required": ("任务需要你的确认", "《{name}》正在等待你的确认，点击处理。", "Your approval is needed", '“{name}” is waiting for your approval. Tap to review.'),
    "cron_completed": ("定时任务有新结果", "《{name}》有新结果，点击查看。", "Scheduled task has a result", '“{name}” has a new result. Tap to view it.'),
    "cron_failed": ("定时任务执行失败", "《{name}》已停止重试，需要你检查。", "Scheduled task failed", '“{name}” has stopped retrying and needs your attention.'),
    "platform_auth_expired": ("任务需要重新授权", "{name} 的授权已失效，相关任务需要你重新授权。", "Authorization needed", 'Authorization for {name} expired. An affected task needs you to sign in again.'),
    "publish_done": ("作品已发布", "《{name}》已由平台确认发布，点击查看。", "Publication confirmed", 'The platform confirmed “{name}” was published. Tap to view it.'),
    "publish_failed": ("作品发布失败", "《{name}》发布失败，点击查看并处理。", "Publication failed", 'The platform reported that “{name}” failed to publish. Tap to review.'),
}


async def emit(db, *, user_id, workspace_id, kind, event_key, name="", session_id=None,
               action_id=None, guard=None, ttl_seconds=3600):
    # Notifications must not turn a valid business commit into an access error
    # if the initiating user left the workspace while the task was running.
    if not await can_receive(db, user_id, workspace_id, session_id):
        return None
    device = await db.get(PushDevice, user_id)
    zh = not device or device.locale.startswith("zh")
    title_zh, body_zh, title_en, body_en = TEMPLATES[kind]
    name = " ".join((name or ("任务" if zh else "Task")).split())[:80]
    return await enqueue_notification(db, user_id=user_id, workspace_id=workspace_id,
        session_id=session_id, action_id=action_id, event_key=event_key, kind=kind,
        title=title_zh if zh else title_en, body=(body_zh if zh else body_en).format(name=name),
        guard=guard, ttl_seconds=ttl_seconds)


async def task_finished(db, session, ticket, *, failed=False):
    if session.parent_id or session.kind == "cron":
        return
    await emit(db, user_id=ticket.user_id, workspace_id=session.workspace_id,
        session_id=session.id, kind="task_failed" if failed else "task_completed",
        event_key=f"task:{ticket.run_id}:terminal", name=session.title,
        guard={"kind": "terminal", "generation": ticket.generation, "failed": failed})


async def question_waiting(db, session, question):
    if session.parent_id or session.kind == "cron":
        return
    kind = "input_required" if question.continuation.get("kind") == "question" else "approval_required"
    await emit(db, user_id=question.user_id, workspace_id=session.workspace_id, session_id=session.id,
        kind=kind, event_key=f"question:{question.id}", name=session.title, action_id=question.id,
        guard={"kind": "question", "id": question.id})


async def cancel_event(db, user_id, event_key):
    await lock_mutation(db)
    ids = select(PushMessage.id).where(PushMessage.user_id == user_id, PushMessage.event_key == event_key)
    await db.execute(update(PushDelivery).where(PushDelivery.message_id.in_(ids),
        PushDelivery.status.in_(("pending", "sending"))).values(
        status="cancelled", error="action_resolved", lease_id=None, lease_until=None))


async def permission_waiting(request, ticket):
    from db.base import get_db_session
    if not ticket:
        return
    async with get_db_session() as db:
        session = await db.get(Session, request.session_id)
        execution = await db.get(SessionExecution, request.session_id)
        if (not session or session.parent_id or session.kind == "cron" or not execution
                or execution.run_id != ticket.run_id or execution.generation != ticket.generation):
            return
        await emit(db, user_id=request.user_id, workspace_id=session.workspace_id, session_id=session.id,
            kind="approval_required", event_key=f"permission:{request.id}", name=session.title,
            action_id=request.id, ttl_seconds=300,
            guard={"kind": "permission", "runId": ticket.run_id, "generation": ticket.generation})


async def permission_resolved(request_id, user_id):
    from db.base import get_db_session
    async with get_db_session() as db:
        await cancel_event(db, user_id, f"permission:{request_id}")


async def cron_result(db, job, result, *, terminal_failure=False):
    from cron.i18n import is_silent
    if (job.delivery or {}).get("notifications_enabled", True) is False:
        return
    status, run_id = result.get("status"), result.get("run_id")
    if not run_id:
        from db.models.cron import CronRun
        run_id = await db.scalar(select(CronRun.id).where(CronRun.job_id == job.id)
                                 .order_by(CronRun.started_at.desc()).limit(1))
    if not run_id:
        return
    if status == "ok":
        text = result.get("summary_text", "")
        if not text.strip() or is_silent(text):
            return
        kind = "cron_completed"
    elif status == "error" and terminal_failure:
        kind = "cron_failed"
    else:
        return
    await emit(db, user_id=job.user_id, workspace_id=job.workspace_id, session_id=job.session_id,
        kind=kind, event_key=f"cron:{run_id}:terminal", name=job.name, action_id=job.id,
        guard={"kind": "cron", "id": run_id, "jobId": job.id, "status": status})


async def publish_result(db, job):
    # Only a committed, authoritative platform result may call this helper.
    if job.status not in {"published", "failed"}:
        return
    # The desktop route currently uses "published" for list/readback or
    # review-pending, and "failed" for transport ambiguity. Neither proves a
    # platform terminal result. Do not turn those into a false push claim.
    if job.platform == "douyin_creator":
        return
    await emit(db, user_id=job.user_id, workspace_id=job.workspace_id,
        kind="publish_done" if job.status == "published" else "publish_failed",
        event_key=f"publish:{job.id}:terminal", name=job.title, action_id=job.id,
        guard={"kind": "publish", "id": job.id, "status": job.status})


async def auth_blocked(db, account, *, session_id=None, user_id=None):
    from db.models.publish_job import PublishJob
    # A background token-maintenance sweep alone must not push to a workspace.
    if session_id and user_id:
        session = await db.get(Session, session_id)
        execution = await db.get(SessionExecution, session_id)
        if (session and session.user_id == user_id and session.workspace_id == account.workspace_id
                and not session.parent_id and session.kind != "cron" and execution and execution.run_id
                and execution.lease_until and utc(execution.lease_until) > now()):
            await emit(db, user_id=user_id, workspace_id=account.workspace_id, session_id=session_id,
                kind="platform_auth_expired", event_key=f"auth:{account.id}:{execution.run_id}",
                name=account.platform, action_id=account.id,
                guard={"kind": "auth", "id": account.id, "generation": execution.generation})
    jobs = (await db.scalars(select(PublishJob).where(PublishJob.platform_account_id == account.id,
        PublishJob.status == "pending", PublishJob.expires_at > now()))).all()
    for job in jobs:
        await emit(db, user_id=job.user_id, workspace_id=job.workspace_id,
            kind="platform_auth_expired", event_key=f"auth:{account.id}:publish:{job.id}",
            name=account.platform, action_id=account.id,
            guard={"kind": "auth", "id": account.id, "publishId": job.id})


async def guard_valid(db, message):
    guard = message.payload.get("guard") or {}
    kind = guard.get("kind")
    if not kind:
        return True
    session_id = message.payload.get("sessionId")
    if kind == "question":
        row = await db.get(QuestionCheckpoint, guard["id"])
        execution = await db.get(SessionExecution, session_id)
        return bool(row and execution and row.status == "pending" and row.generation == execution.generation
                    and (not row.expires_at or utc(row.expires_at) > now()))
    if kind in {"terminal", "permission"}:
        execution = await db.get(SessionExecution, session_id)
        if not execution or execution.generation != guard["generation"]:
            return False
        if kind == "permission":
            return bool(execution.run_id == guard["runId"] and execution.lease_until
                        and utc(execution.lease_until) > now())
        session = await db.get(Session, session_id)
        return bool(not execution.run_id and not execution.resume_pending and session
                    and session.status == ("error" if guard["failed"] else "idle"))
    if kind == "cron":
        from db.models.cron import CronJob, CronRun
        row, job = await db.get(CronRun, guard["id"]), await db.get(CronJob, guard["jobId"])
        return bool(row and job and row.status == guard["status"]
                    and (job.delivery or {}).get("notifications_enabled", True) is not False)
    if kind == "publish":
        from db.models.publish_job import PublishJob
        row = await db.get(PublishJob, guard["id"])
        return bool(row and row.status == guard["status"])
    if kind == "auth":
        from db.models.platform_account import PlatformAccount
        from db.models.publish_job import PublishJob
        row = await db.get(PlatformAccount, guard["id"])
        if not row or row.deleted_at or row.status != "expired":
            return False
        if session_id:
            execution = await db.get(SessionExecution, session_id)
            return bool(execution and execution.generation == guard["generation"])
        job = await db.get(PublishJob, guard["publishId"])
        return bool(job and job.status == "pending" and utc(job.expires_at) > now())
    return False
