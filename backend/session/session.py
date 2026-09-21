"""Session CRUD operations — backed by SQLAlchemy ORM tables."""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select, tuple_, update

from bus import bus
from bus.events import SESSION_STATUS, SESSION_TITLE
from db.base import get_db_session
from db.models.session import Session as SessionORM
from db.models.message import Message as MessageORM
from db.models.part import Part as PartORM, PRIVATE_TOOL_PART_FIELDS, public_part_data
from models.message import SessionStatus, TokenUsage, MessageWithParts, MessageInfo, MessagePart
from core.identifier import descending, ascending
from core.log import create_logger

log = create_logger("session")

RunFence = tuple[str, str, int]


async def _assert_run_fence(
    db,
    run_fence: RunFence | None,
    *,
    session_id: str,
    user_id: str,
) -> None:
    if run_fence is None:
        return
    fence_session_id, run_id, generation = run_fence
    if fence_session_id != session_id:
        from agent.driver import LeaseLostError

        raise LeaseLostError("agent transcript fence targets another session")
    from agent.driver import assert_run_fence_locked

    await assert_run_fence_locked(
        db,
        session_id=session_id,
        user_id=user_id,
        run_id=run_id,
        generation=generation,
    )


class Session(BaseModel):
    """Session model matching frontend types/session.ts."""
    id: str
    user_id: str = "default"
    workspace_id: str = ""
    owner_username: str = ""
    title: str = ""
    agent: str = "build"
    model: str = ""
    #: Model-owned reasoning effort; None means use the model route's default.
    variant: str | None = None
    #: Video model for this conversation; "" = deployment default.
    video_model: str = ""
    video_resolution: str = ""
    status: SessionStatus = SessionStatus.IDLE
    created_at: str = ""
    updated_at: str = ""
    sandbox_id: str | None = None
    additions: int = 0
    deletions: int = 0
    files_changed: int = 0
    token_usage: TokenUsage | None = None
    slug: str = ""
    # Internal fields
    project_id: str = "default"
    parent_id: str | None = None  # Links child (subtask) sessions to their parent
    kind: str = "normal"  # "normal" | "cron" (cron run transcript)
    # Never serialize private reveal/fallback state through REST, SSE, forks,
    # logs, or the frontend session payload.
    tool_exposure_state: dict = Field(default_factory=dict, exclude=True)


def _orm_to_session(row: SessionORM) -> Session:
    """Convert a SessionORM row to a Pydantic Session model."""
    return Session(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        title=row.title or "",
        agent=row.agent or "build",
        model=row.model or "",
        variant=getattr(row, "variant", None),
        video_model=getattr(row, "video_model", None) or "",
        video_resolution=getattr(row, "video_resolution", None) or "",
        status=SessionStatus(row.status) if row.status else SessionStatus.IDLE,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
        sandbox_id=row.sandbox_id,
        additions=row.additions or 0,
        deletions=row.deletions or 0,
        files_changed=row.files_changed or 0,
        token_usage=TokenUsage(**row.token_usage) if row.token_usage and isinstance(row.token_usage, dict) and any(row.token_usage.values()) else None,
        slug=row.slug or "",
        project_id=row.project_id or "default",
        parent_id=row.parent_id,
        kind=getattr(row, "kind", None) or "normal",
        tool_exposure_state=getattr(row, "tool_exposure_state", None) or {},
    )


def plan_path(session: Session, slug: str = "default") -> str:
    """Where this session's plan file lives, inside its own project.

    Plans used to share one global directory, so two projects planning at once
    wrote into the same place. They belong with the code they describe.
    """
    ts = int(datetime.fromisoformat(session.created_at).timestamp() * 1000)
    from project.workspace import project_directory
    return f"{project_directory(slug)}/.openbox/plans/{ts}-{session.slug}.md"


async def plan_path_for(session: Session) -> str:
    """plan_path with the session's project resolved."""
    from project.workspace import slug_for
    return plan_path(session, await slug_for(session.project_id))


async def create_session(
    model: str = "",
    agent: str = "build",
    variant: str | None = None,
    title: str | None = None,
    parent_id: str | None = None,
    user_id: str = "default",
    workspace_id: str = "",
    project_id: str | None = None,
    kind: str = "normal",
    strict_project: bool = False,
) -> Session:
    """Create a new session."""
    if strict_project:
        from project.workspace import get_project
        if not project_id or await get_project(project_id, user_id, workspace_id or None) is None:
            raise LookupError("project is missing or no longer available")
    from core.slug import create as create_slug

    legacy_scope = False
    if not workspace_id:
        from db.models.user import User
        async with get_db_session() as db:
            workspace_id = (
                await db.execute(
                    select(User.default_workspace_id).where(User.id == user_id)
                )
            ).scalar_one_or_none() or ""
    if not workspace_id:
        workspace_id = "ws_default"
        legacy_scope = True

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    session_id = descending("session")
    slug = create_slug()
    # Empty rather than "New session - <iso timestamp>": the frontend renders
    # its own localized "untitled" placeholder, and a raw ISO string leaking
    # into the sidebar reads as garbage. The title generator fills it in.
    final_title = title or ""

    # A session always belongs to a project; an unrecognised one (or none at
    # all) lands in the user's default rather than failing the request.
    from project.workspace import resolve_for_session
    project_id = (
        await resolve_for_session(project_id, user_id)
        if legacy_scope
        else await resolve_for_session(project_id, user_id, workspace_id)
    )

    async with get_db_session() as db:
        row = SessionORM(
            id=session_id,
            user_id=user_id,
            workspace_id=workspace_id,
            project_id=project_id,
            title=final_title,
            agent=agent,
            model=model,
            variant=variant,
            status="idle",
            slug=slug,
            kind=kind,
            parent_id=parent_id,
            token_usage={},
            tool_exposure_state={},
            created_at=now,
            updated_at=now,
        )
        db.add(row)

    session = Session(
        id=session_id,
        user_id=user_id,
        workspace_id=workspace_id,
        title=final_title,
        agent=agent,
        model=model,
        variant=variant,
        status=SessionStatus.IDLE,
        created_at=now_iso,
        updated_at=now_iso,
        slug=slug,
        project_id=project_id,
        parent_id=parent_id,
        kind=kind,
    )

    bus.publish(SESSION_STATUS, {
        "userId": user_id,
        "sessionId": session.id,
        "status": session.status.value,
    })

    log.info(f"Created session {session.id}")
    return session


def _new_session_record(
    *,
    model: str,
    agent: str,
    variant: str | None = None,
    title: str | None,
    parent_id: str | None,
    user_id: str,
    project_id: str,
    workspace_id: str,
    kind: str = "normal",
    now: datetime | None = None,
    session_id: str | None = None,
) -> tuple[SessionORM, Session]:
    """Build the ORM/public pair used by every Session acceptance path.

    Project authorization remains the caller's responsibility because it must
    happen inside the caller's transaction. Keeping construction here makes a
    Task child byte-for-byte identical to an ordinary Session after that
    authorization has succeeded.
    """
    from core.slug import create as create_slug

    created_at = now or datetime.now(timezone.utc)
    actual_session_id = session_id or descending("session")
    slug = create_slug()
    # Empty rather than "New session - <iso timestamp>": the frontend renders
    # its own localized "untitled" placeholder, and a raw ISO string leaking
    # into the sidebar reads as garbage. The title generator fills it in.
    final_title = title or ""
    row = SessionORM(
        id=actual_session_id,
        user_id=user_id,
        workspace_id=workspace_id,
        project_id=project_id,
        title=final_title,
        agent=agent,
        model=model,
        variant=variant,
        status="idle",
        slug=slug,
        kind=kind,
        parent_id=parent_id,
        token_usage={},
        tool_exposure_state={},
        created_at=created_at,
        updated_at=created_at,
    )
    public = Session(
        id=actual_session_id,
        title=final_title,
        agent=agent,
        model=model,
        variant=variant,
        status=SessionStatus.IDLE,
        created_at=created_at.isoformat(),
        updated_at=created_at.isoformat(),
        slug=slug,
        user_id=user_id,
        workspace_id=workspace_id,
        project_id=project_id,
        parent_id=parent_id,
        kind=kind,
    )
    return row, public


def _publish_session_created(session: Session) -> None:
    bus.publish(SESSION_STATUS, {
        "userId": session.user_id,
        "sessionId": session.id,
        "status": session.status.value,
    })


async def project_id_for(session_id: str) -> str:
    """The project a session belongs to, looked up by session id alone.

    get_session() scopes by user_id and so needs a caller that knows it. Some
    internals — the snapshot store, the sandbox workdir — only ever hold a
    session id, and defaulting the user there silently filed their work under
    the wrong project. This is not an authorization boundary: it returns a
    project id for a session the caller is already acting on.
    """
    async with get_db_session() as db:
        return (await db.execute(
            select(SessionORM.project_id).where(SessionORM.id == session_id)
        )).scalar_one_or_none() or ""


async def workspace_identity_for(session_id: str) -> tuple[str, str]:
    """Return ``(user_id, project_id)`` for internal path resolution.

    This does not grant access; callers already own a session/sandbox handle.
    Keeping both values together prevents a project lookup fallback from ever
    silently moving one tenant's snapshots into another tenant's namespace.
    """
    async with get_db_session() as db:
        row = (await db.execute(
            select(SessionORM.user_id, SessionORM.project_id).where(
                SessionORM.id == session_id
            )
        )).one_or_none()
    if not row:
        return "default", "default"
    return (row[0] or "default", row[1] or "default")


async def get_session(
    session_id: str,
    project_id: str = "default",
    user_id: str = "default",
    workspace_id: str | None = None,
) -> Session | None:
    """Get a session by ID (user_id required for ownership check)."""
    async with get_db_session() as db:
        conditions = [
            SessionORM.id == session_id,
            SessionORM.user_id == user_id,
            SessionORM.is_deleted == False,
        ]
        if workspace_id:
            conditions.append(SessionORM.workspace_id == workspace_id)
        result = await db.execute(select(SessionORM).where(*conditions))
        row = result.scalar_one_or_none()
        if not row:
            return None
        return _orm_to_session(row)


async def get_session_in_workspace(session_id: str, workspace_id: str) -> Session | None:
    """Read a session through workspace membership rather than ownership."""
    from db.models.user import User

    async with get_db_session() as db:
        result = (
            await db.execute(
                select(SessionORM, User.username)
                .outerjoin(User, User.id == SessionORM.user_id)
                .where(
                    SessionORM.id == session_id,
                    SessionORM.workspace_id == workspace_id,
                    SessionORM.is_deleted == False,
                )
            )
        ).one_or_none()
        if result is None:
            return None
        row, username = result
        item = _orm_to_session(row)
        item.owner_username = username or row.user_id
        return item


async def list_sessions(
    project_id: str | None = None,
    user_id: str = "default",
    workspace_id: str | None = None,
) -> list[Session]:
    """Top-level sessions for a workspace, optionally narrowed to one project.

    Passing no project returns every session, which is what the "All projects"
    view in the sidebar shows.
    """
    from sqlalchemy import or_
    from db.models.user import User

    conditions = [
        SessionORM.is_deleted == False,  # noqa: E712
        SessionORM.kind != "agent_draft",
        # Top-level chats, plus cron run sessions: those show in the sidebar
        # (clock-badged) even though chat-created ones carry a parent_id.
        # Subagent (task-tool) children stay hidden.
        or_(
            SessionORM.parent_id == None,  # noqa: E711
            SessionORM.kind == "cron",
        ),
    ]
    conditions.append(
        SessionORM.workspace_id == workspace_id
        if workspace_id
        else SessionORM.user_id == user_id
    )
    if project_id:
        conditions.append(SessionORM.project_id == project_id)
    async with get_db_session() as db:
        result = await db.execute(
            select(SessionORM, User.username)
            .outerjoin(User, User.id == SessionORM.user_id)
            .where(*conditions)
            .order_by(SessionORM.created_at.desc())
        )
        items = []
        for row, username in result.all():
            item = _orm_to_session(row)
            item.owner_username = username or row.user_id
            items.append(item)
        return items


async def delete_session(
    session_id: str,
    user_id: str = "default",
    workspace_id: str | None = None,
) -> bool:
    """Soft-delete a session (messages/parts are kept for history).

    Also cascade-disables associated cron jobs.
    """
    now = datetime.now(timezone.utc)

    # Establish ownership before *any* side effect. Previously a caller that
    # guessed another tenant's session id could detach its cron notifications
    # and release its in-memory sandbox binding even though the scoped session
    # update itself matched no row.
    from session.internal_parts import (
        begin_session_write,
        clear_internal_session_locked,
        lock_owned_session,
        session_exposure_lock,
    )

    async with session_exposure_lock(session_id):
        async with get_db_session() as db:
            await begin_session_write(db)
            try:
                row = await lock_owned_session(
                    db,
                    session_id,
                    user_id,
                    include_deleted=True,
                )
            except LookupError:
                return False
            if workspace_id and row.workspace_id != workspace_id:
                return False
            from team.errors import TeamError
            if row.kind == "team_member":
                raise TeamError("TEAM_MEMBER_READ_ONLY", "Team member sessions belong to their root conversation.")
            from team.retention import delete_with_root_locked
            deleted_members = await delete_with_root_locked(db, row, now)
            from question import runtime
            execution = await runtime.execution_locked(db, session_id, user_id)
            invalidated_questions = await runtime.invalidate_locked(db, execution, "cancelled")
            execution.run_id = None
            execution.lease_until = None
            await clear_internal_session_locked(db, row)
            row.is_deleted = True
            row.deleted_at = now
            row.updated_at = now
            # Written whether or not recording is on: the worker tombstones and
            # purges the trajectory once the deletion has committed.
            from trajectory import emit_control
            from trajectory.types import iso
            emit_control({"type": "session.deleted", "session_id": session_id, "user_id": user_id,
                          "deleted_at": iso(now)}, db=db)
    runtime.publish_invalidated(invalidated_questions)
    from session.status import trigger_abort
    trigger_abort(session_id)
    for member_id in deleted_members:
        trigger_abort(member_id)
    # Cascade: cron jobs are project-scoped and outlive conversations. The
    # deleted session merely stops being their notify target.
    try:
        from db.models.cron import CronJob
        async with get_db_session() as db:
            await db.execute(
                update(CronJob)
                .where(
                    CronJob.session_id == session_id,
                    CronJob.user_id == user_id,
                    CronJob.is_deleted == False,
                )
                .values(session_id=None, updated_at=now)
            )
    except Exception as exc:
        log.debug(f"Cron cascade cleanup: {type(exc).__name__}")

    # Release sandbox
    from sandbox import sandbox_manager
    await sandbox_manager.release(session_id, user_id=user_id)
    for member_id in deleted_members:
        await sandbox_manager.release(member_id, user_id=user_id)

    log.info(f"Deleted session {session_id}")
    return True


async def update_session(
    session_id: str,
    user_id: str = "default",
    *,
    run_fence: RunFence | None = None,
    **kwargs,
) -> Session | None:
    """Update session fields."""
    # Serialize TokenUsage to dict for JSON column
    if "token_usage" in kwargs and kwargs["token_usage"] is not None:
        tu = kwargs["token_usage"]
        if hasattr(tu, "model_dump"):
            kwargs["token_usage"] = tu.model_dump()

    # Serialize status enum to string
    if "status" in kwargs and kwargs["status"] is not None:
        s = kwargs["status"]
        if hasattr(s, "value"):
            kwargs["status"] = s.value

    kwargs["updated_at"] = datetime.now(timezone.utc)

    from question import runtime
    # A run (or its title/suggestions work) bound to this session may only
    # write while it still holds the turn. Token and context counters record
    # consumption that already happened, so they ignore revocation.
    ticket = runtime.bound_ticket(session_id) if set(kwargs) - {"token_usage", "updated_at"} else None
    if ticket is not None and isinstance(ticket, runtime.RunTicket):
        runtime.assert_not_revoked("session", ticket)

    async with get_db_session() as db:
        setting_keys = set(kwargs) & {"title", "model", "variant", "agent", "project_id", "directory", "revert"}
        await _assert_run_fence(db, run_fence, session_id=session_id, user_id=user_id)
        previous = await db.get(SessionORM, session_id) if setting_keys else None
        before = {key: getattr(previous, key, None) for key in setting_keys} if previous else {}
        statement = update(SessionORM).where(
            SessionORM.id == session_id,
            SessionORM.user_id == user_id,
        )
        if ticket is not None:
            statement = statement.where(runtime.write_fence(ticket)).execution_options(
                synchronize_session=False)
        result = await db.execute(statement.values(**kwargs))
        if ticket is not None and result.rowcount == 0:
            owned = await db.scalar(select(SessionORM.id).where(
                SessionORM.id == session_id, SessionORM.user_id == user_id))
            reason = await runtime.write_refusal(db, ticket) if owned else None
            if reason is not None:
                raise runtime.RunRevoked(ticket, reason, "session")
        if setting_keys and previous is not None and previous.user_id == user_id:
            # This write holds no session row lock: it leaves the recording markers
            # alone and records nothing while they report a pause.
            await record_projection_in_tx(db, session_id, user_id, "session.settings_changed",
                {"before": before, "after": {key: kwargs[key] for key in setting_keys}}, locked=False)

    # Re-fetch to return the updated session
    return await get_session(session_id, user_id=user_id)


async def set_session_status(
    session_id: str,
    status: SessionStatus,
    user_id: str = "default",
    *,
    generation: int | None = None,
    run_fence: RunFence | None = None,
) -> None:
    """Update session status and broadcast SSE event."""
    await update_session(
        session_id,
        user_id=user_id,
        status=status,
        run_fence=run_fence,
    )
    payload = {
        "userId": user_id,
        "sessionId": session_id,
        "status": status.value,
    }
    if generation is not None:
        payload["generation"] = generation
    bus.publish(SESSION_STATUS, payload)


async def set_session_title(session_id: str, title: str, user_id: str = "default") -> None:
    """Update session title and broadcast SSE event."""
    await update_session(session_id, user_id=user_id, title=title)
    bus.publish(SESSION_TITLE, {
        "userId": user_id,
        "sessionId": session_id,
        "title": title,
    })


async def update_session_tokens(
    session_id: str,
    step_tokens: TokenUsage,
    user_id: str = "default",
    *,
    run_fence: RunFence | None = None,
) -> None:
    """Accumulate step-level tokens into session-level token_usage and broadcast."""
    session = await get_session(session_id, user_id=user_id)
    if not session:
        return

    # Get or initialize cumulative usage
    cu = session.token_usage or TokenUsage()
    cu.input += step_tokens.input
    cu.output += step_tokens.output
    cu.cache += step_tokens.cache
    cu.total += step_tokens.total or (step_tokens.input + step_tokens.output)
    cu.cost += step_tokens.cost
    if step_tokens.credits is not None:
        from decimal import Decimal
        cu.credits = str(Decimal(cu.credits or "0") + Decimal(step_tokens.credits))

    # Context window = last step's total tokens (input + output), matching opencode's isOverflow
    cu.context = step_tokens.total or (step_tokens.input + step_tokens.output)

    # Always update limit based on current model (supports model switching)
    from agent.compaction import get_model_context_limit
    cu.limit = get_model_context_limit(session.model) if session.model else 200_000

    await update_session(
        session_id,
        user_id=user_id,
        run_fence=run_fence,
        token_usage=cu,
    )

    from bus.events import SESSION_UPDATED
    payload = {
        "userId": user_id,
        "sessionId": session_id,
        "token_usage": cu.model_dump(),
    }
    if run_fence is not None:
        payload["generation"] = run_fence[2]
    bus.publish(SESSION_UPDATED, payload)


async def update_session_context(
    session_id: str, context: int, limit: int, user_id: str = "default"
) -> TokenUsage | None:
    """Set only the context/limit fields of session token_usage and broadcast.

    The cumulative input/output/cache/total/cost fields are read fresh from the
    database and written back untouched.  Callers must not write a stale
    ``token_usage`` snapshot they loaded earlier — that used to wipe out the
    totals accumulated by ``update_session_tokens`` on previous steps.
    """
    session = await get_session(session_id, user_id=user_id)
    if not session:
        return None

    cu = session.token_usage or TokenUsage()
    cu.context = context
    cu.limit = limit

    await update_session(session_id, user_id=user_id, token_usage=cu)

    from bus.events import SESSION_UPDATED
    bus.publish(SESSION_UPDATED, {
        "userId": user_id,
        "sessionId": session_id,
        "token_usage": cu.model_dump(),
    })
    return cu


async def trajectory_context_in_tx(db, session_id: str, user_id: str, **ids):
    """Resolve the recorded identity of a write; None when the write is not recorded.

    Whether the writer may still write is decided by question.runtime, with
    recording on or off, before this is reached. An identity that belongs to
    another owner or session is not recorded rather than refused.
    """
    from trajectory import TraceContext, current, enabled
    from trajectory.producers import identity, session_context
    if not enabled(user_id):
        return None
    inherited = current()
    if inherited is not None:
        if inherited.user_id != user_id:
            return None
        if inherited.source_session_id == session_id:
            return inherited.derive(**ids)
    from db.models.question import SessionExecution
    execution = await db.get(SessionExecution, session_id)
    # Legacy executions may store {} instead of NULL before capture is enabled;
    # both mean there is no saved identity. Recording markers are not identity.
    saved = identity(execution.trace_context if execution is not None else None)
    if saved:
        try:
            inherited = TraceContext.from_dict(saved)
        except (TypeError, ValueError):
            return None
        if inherited.user_id != user_id or inherited.source_session_id != session_id:
            return None
        return inherited.derive(**ids)
    return await session_context(db, user_id, session_id, **ids)


async def record_projection_in_tx(db, session_id: str, user_id: str, event_type: str, data: dict,
                                   *, locked: bool = True, **ids):
    """Keep the compatibility mutation and its immutable fact in one commit.

    The fact is enqueued only when the write commits. A ``locked`` write holds
    the session row lock and also keeps the recording markers (SPEC §5.6): the
    first recorded activity, a pause while recording is off, a resume after it.
    A write without the lock records nothing while the markers report a pause.
    """
    from trajectory import record
    context = await trajectory_context_in_tx(db, session_id, user_id, **ids)
    if locked:
        from db.models.question import SessionExecution
        from trajectory.producers import mark_recording_in_tx
        await mark_recording_in_tx(db, await db.get(SessionExecution, session_id), user_id=user_id,
                                   session_id=session_id, context=context)
    if context is None:
        return None
    if not locked:
        from trajectory.producers import paused_in_tx
        if await paused_in_tx(db, context):
            return None
    part = data.get("part") or {}
    if event_type == "part.committed" and not data.get("role"):
        role = await db.scalar(select(MessageORM.role).where(
            MessageORM.id == (ids.get("message_id") or part.get("message_id")),
            MessageORM.session_id == session_id, MessageORM.user_id == user_id))
        data = {**data, "role": role}
    if event_type == "part.committed" and part.get("type") == "file" and part.get("asset_id"):
        from trajectory.artifacts import capture_asset_ids_in_tx
        data = {**data, "artifacts": await capture_asset_ids_in_tx(db, context, [part["asset_id"]],
                                                                   role="attachment")}
    await record(event_type, data, context=context, db=db)
    if event_type == "part.committed" and part.get("type") == "plan":
        await record("plan.changed", {"plan_id": part.get("id"), "plan": part}, context=context, db=db)
    return None


async def baseline_snapshot(db, context, session_row) -> dict:
    """The public history a recording period starts from (SPEC §5.6).

    File attachments become asset references; their artifact facts wait for
    the same commit as the baseline.
    """
    messages = (await db.scalars(select(MessageORM).where(
        MessageORM.session_id == context.source_session_id,
        MessageORM.user_id == context.user_id,
    ).order_by(MessageORM.created_at, MessageORM.id))).all()
    parts = (await db.scalars(select(PartORM).where(
        PartORM.session_id == context.source_session_id,
        PartORM.user_id == context.user_id,
    ).order_by(PartORM.created_at, PartORM.id))).all()
    grouped = {}
    for part in parts:
        grouped.setdefault(part.message_id, []).append(public_part_data(part.data))
    baseline = {
        "legacy_session": bool(messages), "source_session_id": context.source_session_id,
        "history": [{"id": msg.id, "role": msg.role, "parent_id": msg.parent_id,
                     "model": msg.model_id or msg.model, "agent": msg.agent,
                     "finish": msg.finish, "summary": msg.summary,
                     "parts": grouped.get(msg.id, [])} for msg in messages],
        "settings": {key: getattr(session_row, key, None) for key in
                     ("model", "agent", "variant", "project_id", "workspace_id")},
        "recording_boundary": "new_activity_only",
    }
    asset_ids = [part.data["asset_id"] for part in parts
                 if isinstance(part.data, dict) and part.data.get("type") == "file" and part.data.get("asset_id")]
    if asset_ids:
        from trajectory.artifacts import capture_asset_ids_in_tx
        baseline["artifacts"] = await capture_asset_ids_in_tx(db, context, asset_ids, role="baseline_input")
    return baseline


# --- Message Operations ---


class CronInjectionDeferred(RuntimeError):
    """The main session is actively owned by an Agent run."""


async def inject_cron_message_pair_once(
    session_id: str,
    user_id: str,
    *,
    delivery_id: str,
    user_message_id: str,
    user_part_id: str,
    assistant_message_id: str,
    assistant_part_id: str,
    user_text: str,
    result_text: str,
    created_at: datetime,
    run_fence: RunFence | None = None,
) -> bool:
    """Atomically inject one idempotent Cron user/assistant message pair.

    The outbox persists all four stable row IDs.  A retry after an ambiguous
    process crash either observes the complete committed pair or inserts the
    complete pair in one transaction; it can never create a second copy or a
    partial transcript.  Without an Agent run fence this control-plane writer
    only mutates an idle session.

    Returns ``True`` when this call inserted the pair and ``False`` for an
    idempotent replay of the already-committed receipt.
    """
    import hashlib
    from datetime import timedelta

    from session.agent_event_log import (
        append_message_events_locked,
        append_part_event_locked,
        ensure_surface_seed_locked,
        prepare_agent_event_write,
    )

    receipt = "cron:" + hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()[:48]
    if len({user_message_id, assistant_message_id}) != 2:
        raise ValueError("Cron delivery message IDs must be distinct")
    if len({user_part_id, assistant_part_id}) != 2:
        raise ValueError("Cron delivery part IDs must be distinct")

    user_part_data = {
        "type": "text",
        "id": user_part_id,
        "text": user_text,
        "channel": None,
        "session_id": session_id,
        "message_id": user_message_id,
        "synthetic": True,
        "ignored": False,
    }
    assistant_part_data = {
        "type": "text",
        "id": assistant_part_id,
        "text": result_text,
        "channel": "final",
        "session_id": session_id,
        "message_id": assistant_message_id,
        "synthetic": False,
        "ignored": False,
    }
    assistant_created_at = created_at + timedelta(microseconds=1)

    inserted = False
    async with get_db_session() as db:
        session_row = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=run_fence,
        )
        if run_fence is None and session_row.status in {
            SessionStatus.BUSY.value,
            SessionStatus.RETRY.value,
            SessionStatus.COMPACTING.value,
        }:
            raise CronInjectionDeferred(
                f"session {session_id} is active; Cron delivery deferred"
            )

        messages = list((await db.execute(
            select(MessageORM).where(
                MessageORM.id.in_([user_message_id, assistant_message_id])
            )
        )).scalars().all())
        parts = list((await db.execute(
            select(PartORM).where(
                PartORM.id.in_([user_part_id, assistant_part_id])
            )
        )).scalars().all())

        if messages or parts:
            by_message = {row.id: row for row in messages}
            by_part = {row.id: row for row in parts}
            user_row = by_message.get(user_message_id)
            assistant_row = by_message.get(assistant_message_id)
            user_part = by_part.get(user_part_id)
            assistant_part = by_part.get(assistant_part_id)
            valid = bool(
                user_row
                and assistant_row
                and user_part
                and assistant_part
                and user_row.session_id == session_id
                and user_row.user_id == user_id
                and user_row.role == "user"
                and user_row.client_message_id == receipt
                and assistant_row.session_id == session_id
                and assistant_row.user_id == user_id
                and assistant_row.role == "assistant"
                and assistant_row.parent_id == user_message_id
                and assistant_row.agent == "cron"
                and assistant_row.finish == "stop"
                and user_part.message_id == user_message_id
                and user_part.session_id == session_id
                and user_part.user_id == user_id
                and user_part.data == user_part_data
                and assistant_part.message_id == assistant_message_id
                and assistant_part.session_id == session_id
                and assistant_part.user_id == user_id
                and assistant_part.data == assistant_part_data
            )
            if not valid:
                raise ValueError("Cron delivery receipt conflicts with transcript rows")
            return False

        await ensure_surface_seed_locked(db, session_row)
        user_row = MessageORM(
            id=user_message_id,
            session_id=session_id,
            user_id=user_id,
            role="user",
            client_message_id=receipt,
            agent="cron",
            created_at=created_at,
        )
        user_part = PartORM(
            id=user_part_id,
            message_id=user_message_id,
            session_id=session_id,
            user_id=user_id,
            type="text",
            data=user_part_data,
            created_at=created_at,
        )
        assistant_row = MessageORM(
            id=assistant_message_id,
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            parent_id=user_message_id,
            agent="cron",
            created_at=assistant_created_at,
        )
        assistant_part = PartORM(
            id=assistant_part_id,
            message_id=assistant_message_id,
            session_id=session_id,
            user_id=user_id,
            type="text",
            data=assistant_part_data,
            created_at=assistant_created_at,
        )
        db.add_all([user_row, user_part, assistant_row, assistant_part])
        await db.flush()

        await append_message_events_locked(
            db, session_row, user_row, operation="created", run_fence=run_fence
        )
        await append_part_event_locked(
            db,
            session_row,
            user_part,
            user_row,
            operation="created",
            run_fence=run_fence,
        )
        await append_message_events_locked(
            db,
            session_row,
            assistant_row,
            operation="created",
            run_fence=run_fence,
        )
        await append_part_event_locked(
            db,
            session_row,
            assistant_part,
            assistant_row,
            operation="created",
            run_fence=run_fence,
        )
        assistant_row.finish = "stop"
        await db.flush()
        await append_message_events_locked(
            db,
            session_row,
            assistant_row,
            operation="updated",
            run_fence=run_fence,
        )
        inserted = True

    if inserted:
        from bus.events import MESSAGE_CREATED, MESSAGE_UPDATED, PART_CREATED

        generation = run_fence[2] if run_fence is not None else None
        base = {"userId": user_id, "sessionId": session_id}
        user_payload = {
            **base,
            "message": {
                "id": user_message_id,
                "session_id": session_id,
                "role": "user",
                "parts": [user_part_data],
                "created_at": created_at.isoformat(),
                "client_message_id": receipt,
                "agent": "cron",
            },
        }
        assistant_payload = {
            **base,
            "message": {
                "id": assistant_message_id,
                "session_id": session_id,
                "role": "assistant",
                "parts": [],
                "created_at": assistant_created_at.isoformat(),
                "parent_id": user_message_id,
                "agent": "cron",
            },
        }
        part_payload = {
            **base,
            "messageId": assistant_message_id,
            "part": {
                key: value
                for key, value in assistant_part_data.items()
                if key not in {"session_id", "message_id"}
            },
        }
        finish_payload = {
            **base,
            "message": {
                "id": assistant_message_id,
                "role": "assistant",
                "finish": "stop",
            },
        }
        if generation is not None:
            for payload in (
                user_payload,
                assistant_payload,
                part_payload,
                finish_payload,
            ):
                payload["generation"] = generation
        bus.publish(MESSAGE_CREATED, user_payload)
        bus.publish(MESSAGE_CREATED, assistant_payload)
        bus.publish(PART_CREATED, part_payload)
        bus.publish(MESSAGE_UPDATED, finish_payload)
    return inserted

async def create_user_message(
    session_id: str,
    text: str,
    agent: str = "build",
    model: str | None = None,
    synthetic: bool = False,
    variant: str | None = None,
    client_message_id: str | None = None,
    output_format: dict | None = None,
    user_id: str = "default",
    *,
    run_fence: RunFence | None = None,
    bind_trigger: bool = False,
    message_id: str | None = None,
    additional_parts: tuple[MessagePart, ...] = (),
    team_request: dict | None = None,
) -> MessageWithParts:
    """Create a user message with a text part.

    Args:
        synthetic: If True, marks the text part as synthetic (system-generated).
            Synthetic messages are not wrapped with "The user sent the following
            message..." in _insert_reminders, matching opencode's behavior.
    """
    if (
        client_message_id
        and client_message_id.startswith(("sjr:", "tabort:", "team:"))
        and not synthetic
    ):
        raise ValueError("client_message_id uses a platform-reserved prefix")
    if bind_trigger and run_fence is None:
        raise ValueError("bind_trigger requires a run fence")

    async with get_db_session() as db:
        msg = await _insert_user_message_locked(
            db,
            session_id=session_id,
            text=text,
            agent=agent,
            model=model,
            synthetic=synthetic,
            variant=variant,
            client_message_id=client_message_id,
            output_format=output_format,
            user_id=user_id,
            run_fence=run_fence,
            bind_trigger=bind_trigger,
            message_id=message_id,
            additional_parts=additional_parts,
            team_request=team_request,
        )
    _publish_user_message(msg, user_id=user_id, run_fence=run_fence)
    return msg


async def _insert_user_message_locked(
    db,
    *,
    session_id: str,
    text: str,
    agent: str,
    model: str | None,
    synthetic: bool,
    variant: str | None,
    client_message_id: str | None,
    output_format: dict | None,
    user_id: str,
    run_fence: RunFence | None,
    logical_turn_id: str | None = None,
    bind_trigger: bool = False,
    message_id: str | None = None,
    additional_parts: tuple[MessagePart, ...] = (),
    session_row: SessionORM | None = None,
    now: datetime | None = None,
    team_source: dict | None = None,
    team_request: dict | None = None,
) -> MessageWithParts:
    """Insert one canonical User Message into an existing transaction."""
    from session.agent_event_log import (
        append_message_events_locked,
        append_part_event_locked,
        ensure_surface_seed_locked,
        prepare_agent_event_write,
    )
    from models.message import TextPart, id_to_iso

    msg_id = message_id or ascending("message")
    text_part_id = ascending("part")
    created_at = now or datetime.now(timezone.utc)
    if team_source is not None:
        from team.input import TeamInputSource
        if not synthetic:
            raise ValueError("team provenance is restricted to synthetic inputs")
        team_source = TeamInputSource.model_validate(team_source).model_dump(mode="json")
    if team_request is not None:
        from agent_catalog.schemas import TeamRequest
        if synthetic or agent != "team":
            raise ValueError("team_request belongs only to a real user selecting team mode")
        team_request = TeamRequest.model_validate(team_request).model_dump(mode="json")
    text_part = TextPart(
        id=text_part_id,
        text=text,
        session_id=session_id,
        message_id=msg_id,
        synthetic=synthetic,
        team_source=team_source,
        team_request=team_request,
    )
    for extra in additional_parts:
        extra_data = extra.model_dump()
        if (
            extra_data.get("session_id") != session_id
            or extra_data.get("message_id") != msg_id
        ):
            raise ValueError("additional user-message parts must target the new message")

    owner = session_row
    if owner is None:
        owner = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=run_fence,
        )
    elif owner.id != session_id or owner.user_id != user_id:
        raise ValueError("user message Session owner mismatch")
    await ensure_surface_seed_locked(db, owner)
    from question import runtime
    from trajectory.producers import identity, mark_recording_in_tx, markers
    execution = await runtime.execution_locked(db, session_id, user_id)
    saved = identity(execution.trace_context)
    trace = await trajectory_context_in_tx(db, session_id, user_id)
    new_turn = not synthetic and runtime.bound_ticket(session_id) is None
    if trace is not None:
        if new_turn and (trace.source_session_id or trace.session_id) == trace.session_id:
            trace = trace.derive(turn_id=logical_turn_id or msg_id, run_id=None,
                                 generation=None, step_id=None, request_id=None,
                                 call_id=None, message_id=msg_id, part_id=text_part_id)
        else:
            trace = trace.derive(message_id=msg_id, part_id=text_part_id)
    await mark_recording_in_tx(db, execution, user_id=user_id, session_id=session_id, context=trace)
    if new_turn:
        invalidated = await runtime.invalidate_locked(db, execution)
        runtime._after_commit(db, lambda: runtime.publish_invalidated(invalidated))
        from session.status import discard_pending_abort
        runtime._after_commit(db, lambda: discard_pending_abort(session_id))
    msg_row = MessageORM(
        id=msg_id,
        session_id=session_id,
        user_id=user_id,
        role="user",
        client_message_id=client_message_id,
        agent=agent,
        model=model,
        variant=variant,
        format=output_format,
        created_at=created_at,
    )
    db.add(msg_row)
    part_row = PartORM(
        id=text_part_id,
        message_id=msg_id,
        session_id=session_id,
        user_id=user_id,
        type="text",
        data=text_part.model_dump(),
        created_at=created_at,
    )
    db.add(part_row)
    extra_rows: list[PartORM] = []
    for extra in additional_parts:
        extra_data = public_part_data(extra.model_dump())
        extra_row = PartORM(
            id=extra.id,
            message_id=msg_id,
            session_id=session_id,
            user_id=user_id,
            type=extra_data.get("type", "text"),
            data=extra_data,
            created_at=created_at,
        )
        db.add(extra_row)
        extra_rows.append(extra_row)
    await db.flush()
    await append_message_events_locked(
        db,
        owner,
        msg_row,
        operation="created",
        run_fence=run_fence,
        logical_turn_id=logical_turn_id,
    )
    await append_part_event_locked(
        db, owner, part_row, msg_row, operation="created", run_fence=run_fence,
    )
    for extra_row in extra_rows:
        await append_part_event_locked(
            db, owner, extra_row, msg_row, operation="created", run_fence=run_fence,
        )
    if trace is not None:
        from trajectory import record
        trace = trace.derive(generation=execution.generation)
        if new_turn or not saved or saved.get("turn_id") == trace.turn_id:
            execution.trace_context = {**markers(execution.trace_context), **trace.to_dict()}
        if new_turn:
            await record("turn.started", {"source": "user_input"}, context=trace, db=db)
        injected = synthetic or bool(trace.parent_agent_id and trace.source_session_id != trace.session_id)
        await record("input.injected" if injected else "input.accepted", {
            "text": text, "synthetic": synthetic, "agent": agent, "model": model,
            "variant": variant, "client_message_id": client_message_id, "output_format": output_format,
        }, context=trace, db=db)
        await record("message.committed", {"message": {"id": msg_id, "role": "user",
            "agent": agent, "model": model, "variant": variant}, "operation": "created"},
            context=trace, db=db)
        for part in (text_part, *additional_parts):
            await record("part.committed", {"part": part.model_dump(), "role": "user", "operation": "created"},
                         context=trace.derive(part_id=part.id), db=db)
    if bind_trigger:
        if run_fence is None:
            raise ValueError("bind_trigger requires a run fence")
        from agent.driver import bind_trigger_message_locked

        _, run_id, generation = run_fence
        await bind_trigger_message_locked(
            db,
            session_id=session_id,
            user_id=user_id,
            run_id=run_id,
            generation=generation,
            message_id=msg_id,
        )
    return MessageWithParts(
        id=msg_id,
        session_id=session_id,
        role="user",
        parts=[text_part, *additional_parts],
        created_at=id_to_iso(msg_id),
        client_message_id=client_message_id,
        agent=agent,
        model=model,
    )


def _publish_user_message(
    msg: MessageWithParts,
    *,
    user_id: str,
    run_fence: RunFence | None,
) -> None:
    from bus.events import MESSAGE_CREATED

    log.info(f"PUBLISHING MESSAGE_CREATED for {msg.id} userId={user_id}")
    payload = {
        "userId": user_id,
        "sessionId": msg.session_id,
        "message": msg.model_dump(),
    }
    if run_fence is not None:
        payload["generation"] = run_fence[2]
    bus.publish(MESSAGE_CREATED, payload)


async def create_assistant_message(
    session_id: str,
    parent_id: str,
    model_id: str | None = None,
    agent: str | None = None,
    user_id: str = "default",
    *,
    run_fence: RunFence | None = None,
) -> MessageInfo:
    """Create an assistant message."""
    msg_id = ascending("message")
    now = datetime.now(timezone.utc)

    info = MessageInfo(
        id=msg_id,
        session_id=session_id,
        role="assistant",
        parent_id=parent_id,
        model_id=model_id,
        agent=agent,
    )

    async with get_db_session() as db:
        from session.agent_event_log import (
            append_message_events_locked,
            ensure_surface_seed_locked,
            prepare_agent_event_write,
        )

        session_row = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=run_fence,
        )
        await ensure_surface_seed_locked(db, session_row)
        msg_row = MessageORM(
            id=msg_id,
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            summary=False,
            parent_id=parent_id,
            model_id=model_id,
            agent=agent,
            created_at=now,
        )
        db.add(msg_row)
        await record_projection_in_tx(db, session_id, user_id, "message.committed",
            {"message": info.model_dump(mode="json"), "operation": "created"}, message_id=msg_id)
        await db.flush()
        await append_message_events_locked(
            db,
            session_row,
            msg_row,
            operation="created",
            run_fence=run_fence,
        )

    # Publish MESSAGE_CREATED so frontend creates the message entry
    # before subsequent part.created / text_delta events arrive
    from bus.events import MESSAGE_CREATED
    from models.message import id_to_iso
    msg = MessageWithParts(
        id=msg_id,
        session_id=session_id,
        role="assistant",
        parts=[],
        created_at=id_to_iso(msg_id),
        parent_id=parent_id,
        model=model_id,
        agent=agent,
    )
    payload = {
        "userId": user_id,
        "sessionId": session_id,
        "message": msg.model_dump(),
    }
    if run_fence is not None:
        payload["generation"] = run_fence[2]
    bus.publish(MESSAGE_CREATED, payload)

    return info


async def update_message_info(
    info: MessageInfo,
    user_id: str = "default",
    *,
    run_fence: RunFence | None = None,
) -> None:
    """Update a message's info in the database."""
    from session.agent_event_log import sanitize_message_error

    values: dict = {}
    if info.tokens:
        values["tokens"] = info.tokens.model_dump()
    if info.finish is not None:
        values["finish"] = info.finish
    if info.error is not None:
        safe_error = sanitize_message_error(info.error)
        values["error"] = safe_error
        info.error = safe_error
    if info.model_id is not None:
        values["model_id"] = info.model_id
    if info.summary is not None:
        values["summary"] = info.summary
    if info.structured is not None:
        values["structured"] = info.structured

    if values:
        async with get_db_session() as db:
            from session.agent_event_log import (
                append_message_events_locked,
                ensure_surface_seed_locked,
                prepare_agent_event_write,
            )

            session_row = await prepare_agent_event_write(
                db,
                session_id=info.session_id,
                user_id=user_id,
                run_fence=run_fence,
            )
            await ensure_surface_seed_locked(db, session_row)
            await db.execute(
                update(MessageORM).where(
                    MessageORM.id == info.id,
                    MessageORM.session_id == info.session_id,
                    MessageORM.user_id == user_id,
                ).values(**values)
            )
            row = (await db.execute(
                select(MessageORM).where(
                    MessageORM.id == info.id,
                    MessageORM.session_id == info.session_id,
                    MessageORM.user_id == user_id,
                )
            )).scalar_one_or_none()
            if row is None:
                raise LookupError("message not found")
            await append_message_events_locked(
                db,
                session_row,
                row,
                operation="updated",
                run_fence=run_fence,
            )
            await record_projection_in_tx(db, info.session_id, user_id, "message.committed",
                {"message": info.model_dump(mode="json"), "operation": "updated"}, message_id=info.id)
            if info.summary and info.finish == "stop":
                await record_projection_in_tx(db, info.session_id, user_id, "context.replaced",
                    {"reason": "compaction", "summary_message_id": info.id,
                     "boundary_message_id": info.parent_id, "applied": True}, message_id=info.id)

    from bus.events import MESSAGE_UPDATED
    # Send key fields so frontend can merge without losing parts
    msg_update: dict = {"id": info.id, "role": info.role.value}
    if info.tokens:
        msg_update["tokens"] = info.tokens.model_dump()
    if info.finish is not None:
        msg_update["finish"] = info.finish
    if info.error is not None:
        msg_update["error"] = info.error
    if info.model_id is not None:
        msg_update["model"] = info.model_id
    payload = {
        "userId": user_id,
        "sessionId": info.session_id,
        "message": msg_update,
    }
    if run_fence is not None:
        payload["generation"] = run_fence[2]
    bus.publish(MESSAGE_UPDATED, payload)


async def save_part(
    part: MessagePart,
    is_new: bool = False,
    *,
    user_id: str,
    run_fence: RunFence | None = None,
) -> None:
    """Save a part to the database and publish event.

    ``user_id`` is required and keyword-only. It is not a detail of logging:
    the bus routes this part's event by it, so a wrong or defaulted value
    delivers the update to nobody and the UI silently stops moving until
    something forces a refetch. That failure has shipped three times — the
    todo card, the plan card, and the aborted-tool row — each time because
    the parameter had a plausible default and a caller left it off.

    Args:
        part: The message part to save.
        is_new: If True, publish PART_CREATED instead of PART_UPDATED.
        user_id: Whose update this is. Required.
    """
    from session.tool_part_identity import tool_part_identity_values

    identity_values = tool_part_identity_values(part)
    from session.agent_event_log import json_safe_copy, sanitize_public_part_data

    part_dict = sanitize_public_part_data(public_part_data(part.model_dump()))
    msg_id = part_dict.get("message_id", "")
    session_id = part_dict.get("session_id", "")

    # PostgreSQL JSONB cannot store actual NUL bytes. Strip only those bytes;
    # a literal ``\\u0000`` code sample is ordinary user content and survives.
    part_dict = json_safe_copy(part_dict)

    async with get_db_session() as db:
        from session.agent_event_log import (
            append_part_event_locked,
            ensure_surface_seed_locked,
            prepare_agent_event_write,
        )

        session_row = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=run_fence,
        )
        await ensure_surface_seed_locked(db, session_row)
        message_row = (await db.execute(
            select(MessageORM).where(
                MessageORM.id == msg_id,
                MessageORM.session_id == session_id,
                MessageORM.user_id == user_id,
            )
        )).scalar_one_or_none()
        if message_row is None:
            raise LookupError("message not found")
        if is_new:
            created_now = False
            row = (await db.execute(
                select(PartORM).where(PartORM.id == part.id)
            )).scalar_one_or_none()
            if row is None:
                row = PartORM(
                    id=part.id,
                    message_id=msg_id,
                    session_id=session_id,
                    user_id=user_id,
                    type=part_dict.get("type", "text"),
                    data=part_dict,
                    **identity_values,
                    created_at=datetime.now(timezone.utc),
                )
                db.add(row)
                await db.flush()
                created_now = True
            else:
                expected_identity = all(
                    getattr(row, key) == value
                    for key, value in identity_values.items()
                )
                if not (
                    row.message_id == msg_id
                    and row.session_id == session_id
                    and row.user_id == user_id
                    and row.type == part_dict.get("type", "text")
                    and row.data == part_dict
                    and expected_identity
                ):
                    raise ValueError("Part create idempotency conflict")
        else:
            row = (await db.execute(
                select(PartORM).where(
                    PartORM.id == part.id,
                    PartORM.message_id == msg_id,
                    PartORM.session_id == session_id,
                    PartORM.user_id == user_id,
                )
            )).scalar_one_or_none()
            if row is None:
                raise LookupError("part not found")
            row.data = part_dict
            for key, value in identity_values.items():
                setattr(row, key, value)
            await db.flush()
        # A repeated create of the same stable Part is an idempotent retry,
        # while a later update that returns A -> B -> A is three real events.
        if not is_new or created_now:
            await append_part_event_locked(
                db,
                session_row,
                row,
                message_row,
                operation="created" if is_new else "updated",
                run_fence=run_fence,
            )
        await record_projection_in_tx(db, session_id, user_id, "part.committed",
            {"part": part_dict, "operation": "created" if is_new else "updated"},
            message_id=msg_id, part_id=part.id)

    # Exclude internal fields from SSE event (frontend doesn't need them)
    sse_dict = sanitize_public_part_data(public_part_data(part.model_dump(exclude={
        "session_id",
        "message_id",
        "state",
        *PRIVATE_TOOL_PART_FIELDS,
    })))

    from bus.events import PART_CREATED, PART_UPDATED
    event_type = PART_CREATED if is_new else PART_UPDATED
    payload = {
        "userId": user_id,
        "sessionId": session_id,
        "messageId": msg_id,
        "part": sse_dict,
    }
    if run_fence is not None:
        payload["generation"] = run_fence[2]
    bus.publish(event_type, payload)


async def get_messages(
    session_id: str,
    offset: int = 0,
    limit: int | None = None,
    user_id: str | None = None,
    *,
    latest: bool = False,
) -> list[MessageWithParts]:
    """Get messages for a session with their parts in a single query.

    Args:
        user_id: If provided, verifies session belongs to this user (defense-in-depth).
        limit: Maximum rows to return. ``None`` loads the complete transcript and
            is the safe default for Agent/context callers.
        latest: Apply ``offset``/``limit`` from the newest end of the transcript,
            then return the selected page in chronological order. Public clients
            use this so a long conversation never hides the current turn.
    """
    async with get_db_session() as db:
        # Defense-in-depth: verify session ownership when user_id is provided
        if user_id:
            ownership = await db.execute(
                select(SessionORM.id).where(
                    SessionORM.id == session_id,
                    SessionORM.user_id == user_id,
                    SessionORM.is_deleted == False,
                )
            )
            if not ownership.scalar_one_or_none():
                return []

        # Internal Agent callers must see the complete transcript.  The old
        # implicit ``limit=200`` selected the *oldest* rows, so after message
        # 200 every new user prompt disappeared from the loop, compaction,
        # abort, fork and Cron delivery paths.
        query = select(MessageORM).where(MessageORM.session_id == session_id)
        if latest:
            query = query.order_by(MessageORM.created_at.desc(), MessageORM.id.desc())
        else:
            query = query.order_by(MessageORM.created_at.asc(), MessageORM.id.asc())
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        msg_result = await db.execute(query)
        messages = list(msg_result.scalars().all())
        if latest:
            messages.reverse()

        if not messages:
            return []

        # A full load reads parts by session: an IN list over every message id
        # outgrows the driver's bind-parameter limit on the longest, cron-fed
        # sessions. A page keeps to its own messages.
        part_filter = (
            PartORM.session_id == session_id if not offset and limit is None
            else PartORM.message_id.in_([m.id for m in messages])
        )
        parts_result = await db.execute(
            select(PartORM).where(part_filter)
            .order_by(PartORM.created_at, PartORM.id)
        )
        all_parts = parts_result.scalars().all()

    return _assemble(session_id, messages, all_parts)


def _assemble(session_id: str, messages, parts) -> list[MessageWithParts]:
    """Attach each message's public part data, keeping both orders as given."""
    parts_by_msg: dict[str, list[dict]] = {}
    for p in parts:
        parts_by_msg.setdefault(p.message_id, []).append(public_part_data(p.data))

    from models.message import id_to_iso
    result = []
    for m in messages:
        tokens_obj = None
        if m.tokens and isinstance(m.tokens, dict):
            tokens_obj = TokenUsage(**m.tokens)

        result.append(MessageWithParts(
            id=m.id,
            session_id=session_id,
            role=m.role,
            parts=parts_by_msg.get(m.id, []),
            created_at=id_to_iso(m.id),
            client_message_id=m.client_message_id,
            agent=m.agent,
            model=m.model_id if m.role == "assistant" else m.model,
            variant=m.variant,
            parent_id=m.parent_id,
            finish=m.finish,
            summary=m.summary,
            tokens=tokens_obj,
            format=m.format,
            structured=m.structured,
            reaction=m.reaction,
            error=m.error,
        ))

    return result


class HistoryCursorGone(LookupError):
    """The message a history page was anchored to is gone (regenerate, revert, delete)."""


class MessageWindow(BaseModel):
    messages: list[MessageWithParts]
    #: Older messages exist before this window; fetch them with ``before``.
    has_more: bool = False


#: Message ids per parts query, far below any driver's bind-parameter limit.
_PART_QUERY_CHUNK = 1_000


async def get_message_window(
    session_id: str,
    *,
    user_id: str | None = None,
    before: str | None = None,
    after: str | None = None,
    turns: int = 20,
) -> MessageWindow:
    """A slice of a session's history for chat views, oldest first within it.

    With no cursor, the newest ``turns`` turns; with ``before``, the ``turns``
    turns preceding that message. A turn starts at a user message, so a long
    run of tool steps never straddles two pages. With ``after``, that message
    and everything newer: a live view's catch-up, which re-reads the anchor
    because its parts may still be changing.

    Opening a long chat used to download every message on every poll —
    about 1 MB a second for a 350-message session during a run (2026-09-14).

    Raises HistoryCursorGone when the anchor message no longer exists.
    """
    turns = max(1, min(turns, 100))
    key = tuple_(MessageORM.created_at, MessageORM.id)
    async with get_db_session() as db:
        if user_id:
            owned = await db.scalar(select(SessionORM.id).where(
                SessionORM.id == session_id,
                SessionORM.user_id == user_id,
                SessionORM.is_deleted == False,
            ))
            if not owned:
                return MessageWindow(messages=[])

        in_session = MessageORM.session_id == session_id
        anchor = None
        if before or after:
            anchor = (await db.execute(
                select(MessageORM.created_at, MessageORM.id)
                .where(in_session, MessageORM.id == (after or before))
            )).first()
            if anchor is None:
                raise HistoryCursorGone(after or before)

        has_more = False
        if after:
            conditions = [in_session, key >= tuple(anchor)]
        else:
            conditions = [in_session, *([key < tuple(anchor)] if anchor else [])]
            starts = (await db.execute(
                select(MessageORM.created_at, MessageORM.id)
                .where(*conditions, MessageORM.role == "user")
                .order_by(MessageORM.created_at.desc(), MessageORM.id.desc())
                .limit(turns)
            )).all()
            # Fewer turns than asked for means this page reaches the start,
            # including anything said before the first user message.
            if len(starts) == turns:
                first = tuple(starts[-1])
                conditions.append(key >= first)
                has_more = await db.scalar(
                    select(MessageORM.id).where(in_session, key < first).limit(1)
                ) is not None

        messages = (await db.execute(
            select(MessageORM).where(*conditions)
            .order_by(MessageORM.created_at, MessageORM.id)
        )).scalars().all()
        ids = [m.id for m in messages]
        parts = []
        for start in range(0, len(ids), _PART_QUERY_CHUNK):
            parts += (await db.execute(
                select(PartORM).where(PartORM.message_id.in_(ids[start:start + _PART_QUERY_CHUNK]))
                .order_by(PartORM.created_at, PartORM.id)
            )).scalars().all()

    return MessageWindow(messages=_assemble(session_id, messages, parts), has_more=has_more)


async def delete_messages_from(
    session_id: str,
    message_id: str,
    *,
    user_id: str = "default",
    replacement_run_id: str | None = None,
    replacement_generation: int | None = None,
) -> str | None:
    """Drop `message_id` and everything after it. Returns the id of the last
    user message that survives, or None if the session has none left.

    This is what "regenerate" is made of: the assistant's attempt is removed so
    the loop answers the same prompt again, rather than the model reading its
    own failed turn as context and apologising for it.

    The live Surface uses a hard delete deliberately: a ``superseded`` flag
    would require every context reader to remember a filter.  Before deletion,
    the complete public branch is appended to ``session_surface_events`` in
    this same transaction, preserving audit and recovery provenance without
    leaking superseded messages back into model context.
    """
    from session.internal_parts import (
        begin_session_write,
        delete_internal_parts_for_messages_locked,
        lock_owned_session,
        session_exposure_lock,
    )

    doomed: list[str] = []
    invalidated_questions = []
    async with session_exposure_lock(session_id):
        async with get_db_session() as db:
            await begin_session_write(db)
            try:
                session_row = await lock_owned_session(db, session_id, user_id)
            except LookupError:
                return None
            run_fence = None
            if replacement_run_id is not None or replacement_generation is not None:
                if (
                    not replacement_run_id
                    or replacement_generation is None
                    or isinstance(replacement_generation, bool)
                    or replacement_generation < 1
                ):
                    raise ValueError("replacement run id and generation must be valid together")
                run_fence = (
                    session_id,
                    replacement_run_id,
                    replacement_generation,
                )
                await _assert_run_fence(
                    db,
                    run_fence,
                    session_id=session_id,
                    user_id=user_id,
                )
            from session.agent_event_log import ensure_surface_seed_locked

            await ensure_surface_seed_locked(db, session_row)
            target = await db.execute(
                select(MessageORM.created_at).where(
                    MessageORM.id == message_id,
                    MessageORM.session_id == session_id,
                    MessageORM.user_id == user_id,
                )
            )
            cutoff = target.scalar_one_or_none()
            if cutoff is None:
                return None

            doomed = list((await db.execute(
                select(MessageORM.id).where(
                    MessageORM.session_id == session_id,
                    MessageORM.user_id == user_id,
                    or_(
                        MessageORM.created_at > cutoff,
                        and_(
                            MessageORM.created_at == cutoff,
                            MessageORM.id >= message_id,
                        ),
                    ),
                ).order_by(MessageORM.created_at, MessageORM.id)
            )).scalars().all())

            if doomed:
                from question import runtime
                execution = await runtime.execution_locked(db, session_id, user_id)
                invalidated_questions = await runtime.invalidate_locked(db, execution)
                from session.agent_event_log import append_surface_remove_locked
                from session.surface_log import append_surface_change_locked

                await append_surface_change_locked(
                    db,
                    session_row,
                    kind="regenerate",
                    anchor_message_id=message_id,
                    hidden_message_ids=doomed,
                    replacement_run_id=replacement_run_id,
                    replacement_generation=replacement_generation,
                )
                await append_surface_remove_locked(
                    db,
                    session_row,
                    message_ids=doomed,
                    run_fence=run_fence,
                )
                # Lock order is session -> private event -> public part/message.
                await delete_internal_parts_for_messages_locked(db, session_row, doomed)
                await db.execute(PartORM.__table__.delete().where(PartORM.message_id.in_(doomed)))
                await db.execute(MessageORM.__table__.delete().where(MessageORM.id.in_(doomed)))
                await record_projection_in_tx(db, session_id, user_id, "history.regenerated",
                    {"from_message_id": message_id, "removed_message_ids": doomed})

            survivor = await db.execute(
                select(MessageORM.id).where(
                    MessageORM.session_id == session_id,
                    MessageORM.user_id == user_id,
                    MessageORM.role == "user",
                ).order_by(MessageORM.created_at.desc(), MessageORM.id.desc()).limit(1)
            )
            last_user = survivor.scalar_one_or_none()
            if last_user is not None and run_fence is not None:
                from agent.driver import bind_trigger_message_locked

                _, run_id, generation = run_fence
                await bind_trigger_message_locked(
                    db,
                    session_id=session_id,
                    user_id=user_id,
                    run_id=run_id,
                    generation=generation,
                    message_id=last_user,
                )

    from question.runtime import publish_invalidated
    publish_invalidated(invalidated_questions)
    log.info(f"Regenerate: dropped {len(doomed)} message(s) from {message_id} in {session_id}")
    return last_user


async def delete_failed_turn(
    session_id: str,
    message_id: str,
    *,
    user_id: str = "default",
) -> int:
    """Remove an errored assistant message, and the prompt that produced it.

    For a failed turn the user has already moved past: it answers nothing, and
    it is not free to keep — an assistant message with no text still rides
    along in every future request as context.

    The prompt goes too, but only when nothing else answered it. Deleting the
    reply alone would leave the question sitting there unanswered, which is a
    worse artefact than the error card was.

    Restricted to messages that actually carry an error, deliberately: this is
    "dismiss a failure", not a general history-rewriting endpoint.  The public
    turn is archived as an append-only Surface event in the delete transaction.
    Returns the number of messages removed, 0 if the id does not qualify.
    """
    from session.internal_parts import (
        begin_session_write,
        delete_internal_parts_for_messages_locked,
        lock_owned_session,
        session_exposure_lock,
    )

    doomed: list[str] = []
    async with session_exposure_lock(session_id):
        async with get_db_session() as db:
            await begin_session_write(db)
            try:
                session_row = await lock_owned_session(db, session_id, user_id)
            except LookupError:
                return 0
            from session.agent_event_log import ensure_surface_seed_locked

            await ensure_surface_seed_locked(db, session_row)
            row = (await db.execute(
                select(MessageORM.id, MessageORM.parent_id, MessageORM.error, MessageORM.role).where(
                    MessageORM.id == message_id,
                    MessageORM.session_id == session_id,
                    MessageORM.user_id == user_id,
                )
            )).one_or_none()
            if row is None or row.role != "assistant" or not row.error:
                return 0

            doomed = [message_id]

            if row.parent_id:
                siblings = (await db.execute(
                    select(MessageORM.id).where(
                        MessageORM.session_id == session_id,
                        MessageORM.user_id == user_id,
                        MessageORM.parent_id == row.parent_id,
                        MessageORM.id != message_id,
                    )
                )).scalars().all()
                if not siblings:
                    doomed.append(row.parent_id)

            from session.surface_log import append_surface_change_locked
            from session.agent_event_log import append_surface_remove_locked

            await append_surface_change_locked(
                db,
                session_row,
                kind="dismiss",
                anchor_message_id=message_id,
                hidden_message_ids=doomed,
            )
            await append_surface_remove_locked(
                db,
                session_row,
                message_ids=doomed,
            )
            await delete_internal_parts_for_messages_locked(db, session_row, doomed)
            await db.execute(PartORM.__table__.delete().where(PartORM.message_id.in_(doomed)))
            await db.execute(MessageORM.__table__.delete().where(MessageORM.id.in_(doomed)))
            await record_projection_in_tx(db, session_id, user_id, "history.reverted",
                {"reason": "dismiss_failed_turn", "removed_message_ids": doomed})

    log.info(f"Dismissed failed turn {message_id} in {session_id} ({len(doomed)} message(s))")
    return len(doomed)


async def set_message_reaction(
    message_id: str,
    session_id: str,
    reaction: str | None,
    *,
    user_id: str = "default",
) -> None:
    """Persist thumbs up/down feedback for an assistant message."""
    if reaction not in {None, "up", "down"}:
        raise ValueError("invalid message reaction")
    async with get_db_session() as db:
        from session.agent_event_log import (
            append_message_events_locked,
            ensure_surface_seed_locked,
            prepare_agent_event_write,
        )

        session_row = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=None,
        )
        await ensure_surface_seed_locked(db, session_row)
        row = (await db.execute(
            select(MessageORM).where(
                MessageORM.id == message_id,
                MessageORM.session_id == session_id,
                MessageORM.user_id == user_id,
            )
        )).scalar_one_or_none()
        if row is None:
            raise LookupError("message not found")
        row.reaction = reaction
        await db.flush()
        await append_message_events_locked(
            db,
            session_row,
            row,
            operation="updated",
            run_fence=None,
        )


async def get_parts_for_message(message_id: str) -> list[dict]:
    """Get all parts for a specific message, ordered by creation time."""
    async with get_db_session() as db:
        result = await db.execute(
            select(PartORM).where(PartORM.message_id == message_id)
            .order_by(PartORM.created_at)
        )
        return [public_part_data(p.data) for p in result.scalars().all()]


async def update_part_data_locked(
    db,
    session_row: SessionORM,
    part_id: str,
    data: dict,
    *,
    user_id: str,
    run_fence: RunFence | None = None,
) -> tuple[dict, str]:
    """Mutate one Part and append its AgentEvent in the caller's transaction.

    ``session_row`` must come from ``prepare_agent_event_write`` so the owning
    Session and, when present, exact Agent generation stay locked until the
    caller commits. This is the gateway for domain workflows that need their
    own SQL writes and a public Part mutation to succeed or roll back together.
    """
    from session.agent_event_log import (
        append_part_event_locked,
        ensure_surface_seed_locked,
    )

    from session.agent_event_log import sanitize_public_part_data

    clean_data = sanitize_public_part_data(public_part_data(data))
    session_id = session_row.id
    payload_session_id = str(clean_data.get("session_id") or "")
    if payload_session_id and payload_session_id != session_id:
        raise ValueError("Part payload crosses its Session owner")
    if session_row.user_id != user_id:
        raise LookupError("session not found")

    await ensure_surface_seed_locked(db, session_row)
    row = (await db.execute(
        select(PartORM).where(
            PartORM.id == part_id,
            PartORM.session_id == session_id,
            PartORM.user_id == user_id,
        )
    )).scalar_one_or_none()
    if row is None:
        raise LookupError("part not found")
    message_row = (await db.execute(
        select(MessageORM).where(
            MessageORM.id == row.message_id,
            MessageORM.session_id == session_id,
            MessageORM.user_id == user_id,
        )
    )).scalar_one_or_none()
    if message_row is None:
        raise LookupError("message not found")
    row.data = clean_data
    await db.flush()
    await append_part_event_locked(
        db,
        session_row,
        row,
        message_row,
        operation="updated",
        run_fence=run_fence,
    )
    return clean_data, row.message_id


async def update_part_data(
    part_id: str,
    data: dict,
    publish: bool = False,
    user_id: str = "default",
    *,
    run_fence: RunFence | None = None,
) -> None:
    """Update a part's data directly by ID.

    Persists silently by default — some callers (compaction pruning) only
    stamp bookkeeping the UI never renders. Pass ``publish=True`` when the
    change is user-visible, e.g. a tool flipping to ``error`` on abort: without
    the event the row spins forever, because the store still holds the stale
    ``running`` copy until something forces a refetch.
    """
    from session.agent_event_log import sanitize_public_part_data

    data = sanitize_public_part_data(public_part_data(data))
    session_id = str(data.get("session_id") or (run_fence[0] if run_fence else ""))
    async with get_db_session() as db:
        from session.agent_event_log import (
            prepare_agent_event_write,
        )

        if not session_id:
            session_id = await db.scalar(select(PartORM.session_id).where(
                PartORM.id == part_id, PartORM.user_id == user_id,
            ))
            if session_id is None:
                raise LookupError("part not found")
        session_row = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=run_fence,
        )
        data, message_id = await update_part_data_locked(
            db,
            session_row,
            part_id,
            data,
            user_id=user_id,
            run_fence=run_fence,
        )
        await record_projection_in_tx(db, session_id, user_id, "part.committed",
            {"part": data, "operation": "updated"}, message_id=message_id, part_id=part_id)
    if not publish:
        return
    sse_dict = {k: v for k, v in data.items() if k not in ("session_id", "message_id", "state")}
    from bus.events import PART_UPDATED
    payload = {
        "userId": user_id,
        "sessionId": session_id,
        "messageId": data.get("message_id", "") or message_id,
        "part": sse_dict,
    }
    if run_fence is not None:
        payload["generation"] = run_fence[2]
    bus.publish(PART_UPDATED, payload)


async def check_message_has_synthetic_text(message_id: str) -> bool:
    """Check if a message already has a synthetic text part."""
    async with get_db_session() as db:
        result = await db.execute(
            select(PartORM).where(
                PartORM.message_id == message_id,
                PartORM.type == "text",
            )
        )
        for p in result.scalars().all():
            if isinstance(p.data, dict) and p.data.get("synthetic"):
                return True
        return False
