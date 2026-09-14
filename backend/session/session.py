"""Session CRUD operations — backed by SQLAlchemy ORM tables."""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy import select, update

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
) -> Session:
    """Create a new session."""
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
            from question import runtime
            execution = await runtime.execution_locked(db, session_id, user_id)
            invalidated_questions = await runtime.invalidate_locked(db, execution, "cancelled")
            execution.run_id = None
            execution.lease_until = None
            await clear_internal_session_locked(db, row)
            from trajectory import delete_trajectory_in_tx
            await delete_trajectory_in_tx(db, session_id, user_id)
            row.is_deleted = True
            row.deleted_at = now
            row.updated_at = now
    runtime.publish_invalidated(invalidated_questions)
    from session.status import trigger_abort
    trigger_abort(session_id)
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

    log.info(f"Deleted session {session_id}")
    return True


async def update_session(session_id: str, user_id: str = "default", **kwargs) -> Session | None:
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

    async with get_db_session() as db:
        setting_keys = set(kwargs) & {"title", "model", "variant", "agent", "project_id", "directory", "revert"}
        previous = await db.get(SessionORM, session_id) if setting_keys else None
        before = {key: getattr(previous, key, None) for key in setting_keys} if previous else {}
        await db.execute(
            update(SessionORM).where(
                SessionORM.id == session_id,
                SessionORM.user_id == user_id,
            ).values(**kwargs)
        )
        if setting_keys and previous is not None and previous.user_id == user_id:
            await record_projection_in_tx(db, session_id, user_id, "session.settings_changed",
                {"before": before, "after": {key: kwargs[key] for key in setting_keys}})

    # Re-fetch to return the updated session
    return await get_session(session_id, user_id=user_id)


async def set_session_status(session_id: str, status: SessionStatus, user_id: str = "default") -> None:
    """Update session status and broadcast SSE event."""
    from question import runtime
    ticket = runtime.current_run.get()
    if ticket and ticket.session_id == session_id:
        async with runtime.transaction(session_id, user_id) as (db, session, execution):
            if not runtime.owns(execution, ticket):
                return
            value = await runtime.waiting_status(db, execution) if status == SessionStatus.IDLE else status.value
            session.status = value
        runtime.publish_status(session_id, user_id, value)
        return
    await update_session(session_id, user_id=user_id, status=status)
    bus.publish(SESSION_STATUS, {
        "userId": user_id,
        "sessionId": session_id,
        "status": status.value,
    })


async def set_session_title(session_id: str, title: str, user_id: str = "default") -> None:
    """Update session title and broadcast SSE event."""
    await update_session(session_id, user_id=user_id, title=title)
    bus.publish(SESSION_TITLE, {
        "userId": user_id,
        "sessionId": session_id,
        "title": title,
    })


async def update_session_tokens(session_id: str, step_tokens: TokenUsage, user_id: str = "default") -> None:
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

    await update_session(session_id, user_id=user_id, token_usage=cu)

    from bus.events import SESSION_UPDATED
    bus.publish(SESSION_UPDATED, {
        "userId": user_id,
        "sessionId": session_id,
        "token_usage": cu.model_dump(),
    })


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
    """Resolve execution ownership without creating history on read-only paths."""
    from trajectory import TraceContext, context_for_session, current, enabled
    if not enabled(user_id):
        return None
    inherited = current()
    if inherited is not None:
        if inherited.user_id != user_id:
            raise ValueError("trajectory owner mismatch")
        if inherited.source_session_id == session_id:
            if inherited.run_id is not None and inherited.generation is not None:
                from db.models.question import SessionExecution
                from trajectory import TrajectoryError
                execution = await db.get(SessionExecution, session_id)
                if execution is not None and (execution.generation != inherited.generation or
                        (execution.run_id is not None and execution.run_id != inherited.run_id)):
                    raise TrajectoryError("Superseded execution cannot update the chat projection")
            return inherited.derive(**ids)
    from db.models.question import SessionExecution
    execution = await db.get(SessionExecution, session_id)
    saved = getattr(execution, "trace_context", None) if execution is not None else None
    # Legacy executions may store {} instead of NULL before capture is enabled.
    # Both mean there is no saved context; resolve ownership from the session.
    if isinstance(saved, dict) and saved:
        inherited = TraceContext.from_dict(saved)
        if inherited.user_id != user_id or inherited.source_session_id != session_id:
            raise ValueError("persisted trajectory owner/session mismatch")
        return inherited.derive(**ids)
    return await context_for_session(db, user_id, session_id, **ids)


async def prepare_trajectory_assets(session_id: str, user_id: str, asset_ids: list[str] | None = None,
                                    *, include_baseline: bool = False,
                                    root_session_id: str | None = None) -> dict[str, bytes]:
    """Read attachment bytes before a session write lock, then recheck in the write."""
    from trajectory import enabled
    if not enabled(user_id):
        return {}
    from db.models.trajectory import SessionTrajectory, TrajectoryEvent
    from trajectory.artifacts import prepare_asset_ids

    identifiers = list(asset_ids or [])
    async with get_db_session() as db:
        session_row = await db.get(SessionORM, session_id)
        if session_row is None or session_row.user_id != user_id or session_row.is_deleted:
            raise ValueError("Attachment session does not belong to the execution scope")
        if root_session_id is None:
            context = await trajectory_context_in_tx(db, session_id, user_id)
            root_session_id = context.session_id if context is not None else session_id
        workspace_id = session_row.workspace_id
        if include_baseline:
            trajectory = await db.scalar(select(SessionTrajectory).where(
                SessionTrajectory.user_id == user_id, SessionTrajectory.session_id == root_session_id))
            baseline_exists = trajectory is not None and trajectory.recording_status != "paused" and await db.get(
                TrajectoryEvent, f"evt_baseline_{trajectory.id}") is not None
            if not baseline_exists:
                parts = (await db.scalars(select(PartORM).where(
                    PartORM.session_id == session_id, PartORM.user_id == user_id,
                    PartORM.type == "file"))).all()
                identifiers.extend(part.data["asset_id"] for part in parts
                    if isinstance(part.data, dict) and part.data.get("asset_id"))
    return await prepare_asset_ids(user_id, workspace_id, identifiers, root_session_id=root_session_id)


async def prepare_trajectory_baseline_assets(session_id: str, user_id: str, *,
                                             root_session_id: str | None = None) -> dict[str, bytes]:
    return await prepare_trajectory_assets(session_id, user_id, include_baseline=True,
                                           root_session_id=root_session_id)


async def record_projection_in_tx(db, session_id: str, user_id: str, event_type: str, data: dict,
                                   *, prepared_assets: dict[str, bytes] | None = None, **ids):
    """Keep the compatibility mutation and its immutable fact in one commit."""
    from trajectory import record
    context = await trajectory_context_in_tx(db, session_id, user_id, **ids)
    if context is not None:
        part = data.get("part") or {}
        if event_type == "part.committed" and not data.get("role"):
            role = await db.scalar(select(MessageORM.role).where(
                MessageORM.id == (ids.get("message_id") or part.get("message_id")),
                MessageORM.session_id == session_id, MessageORM.user_id == user_id))
            data = {**data, "role": role}
        if event_type == "part.committed" and part.get("type") == "file" and part.get("asset_id"):
            from trajectory.artifacts import capture_asset_ids_in_tx
            artifacts = await capture_asset_ids_in_tx(db, context, [part["asset_id"]], role="attachment",
                                                       prepared=prepared_assets)
            data = {**data, "artifacts": artifacts}
        result = await record(event_type, data, context=context, db=db)
        if event_type == "part.committed" and part.get("type") == "plan":
            await record("plan.changed", {"plan_id": part.get("id"), "plan": part}, context=context, db=db)
        return result
    from trajectory import mark_capture_paused_in_tx
    await mark_capture_paused_in_tx(db, user_id, session_id)
    return None


async def capture_trajectory_baseline_in_tx(db, context, session_row, *,
                                            prepared_assets: dict[str, bytes] | None = None):
    """Capture existing public history before accepting the first new activity."""
    if context is None:
        return
    from trajectory import ensure_trajectory_in_tx
    from db.models.trajectory import SessionTrajectory, TrajectoryEvent
    existing = await db.scalar(select(SessionTrajectory).where(
        SessionTrajectory.user_id == context.user_id, SessionTrajectory.session_id == context.session_id,
    ))
    if existing is not None and existing.recording_status != "paused" and await db.get(
            TrajectoryEvent, f"evt_baseline_{existing.id}") is not None:
        return
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
        await ensure_trajectory_in_tx(db, context)
        baseline["artifacts"] = await capture_asset_ids_in_tx(db, context, asset_ids, role="baseline_input",
                                                               prepared=prepared_assets)
    await ensure_trajectory_in_tx(db, context, baseline=baseline)


# --- Message Operations ---

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
) -> MessageWithParts:
    """Create a user message with a text part.

    Args:
        synthetic: If True, marks the text part as synthetic (system-generated).
            Synthetic messages are not wrapped with "The user sent the following
            message..." in _insert_reminders, matching opencode's behavior.
    """
    if (
        client_message_id
        and client_message_id.startswith(("sjr:", "tabort:"))
        and not synthetic
    ):
        raise ValueError("client_message_id uses a platform-reserved prefix")

    msg_id = ascending("message")
    text_part_id = ascending("part")
    now = datetime.now(timezone.utc)

    from models.message import TextPart
    text_part = TextPart(
        id=text_part_id,
        text=text,
        session_id=session_id,
        message_id=msg_id,
        synthetic=synthetic,
    )

    from question import runtime
    invalidated_questions = []
    prepared_assets = await prepare_trajectory_baseline_assets(session_id, user_id)
    async with runtime.transaction(session_id, user_id) as (db, session_row, execution):
        trace = await trajectory_context_in_tx(db, session_id, user_id)
        if trace is not None:
            if not synthetic:
                trace = trace.derive(turn_id=msg_id, run_id=None, generation=None, step_id=None,
                                     request_id=None, call_id=None, message_id=msg_id, part_id=text_part_id)
            else:
                trace = trace.derive(message_id=msg_id, part_id=text_part_id)
            await capture_trajectory_baseline_in_tx(db, trace, session_row, prepared_assets=prepared_assets)
        if not synthetic:
            invalidated_questions = await runtime.invalidate_locked(db, execution)
        msg_row = MessageORM(
            id=msg_id,
            session_id=session_id,
            user_id=user_id,
            role="user",
            summary=False,
            client_message_id=client_message_id,
            agent=agent,
            model=model,
            variant=variant,
            format=output_format,
            created_at=now,
        )
        db.add(msg_row)

        part_row = PartORM(
            id=text_part_id,
            message_id=msg_id,
            session_id=session_id,
            user_id=user_id,
            type="text",
            data=text_part.model_dump(),
            created_at=now,
        )
        db.add(part_row)
        if trace is not None:
            from trajectory import record
            trace = trace.derive(generation=execution.generation)
            saved_turn = (execution.trace_context or {}).get("turn_id")
            if not synthetic or not execution.trace_context or saved_turn == trace.turn_id:
                execution.trace_context = trace.to_dict()
            if not synthetic:
                await record("turn.started", {"source": "user_input"}, context=trace, db=db)
            await record("input.injected" if synthetic else "input.accepted", {
                "text": text, "synthetic": synthetic, "agent": agent, "model": model,
                "variant": variant, "client_message_id": client_message_id, "output_format": output_format,
            }, context=trace, db=db)
            await record("message.committed", {"message": {"id": msg_id, "role": "user",
                "agent": agent, "model": model, "variant": variant}, "operation": "created"},
                context=trace, db=db)
            await record("part.committed", {"part": text_part.model_dump(), "role": "user", "operation": "created"},
                         context=trace, db=db)
        else:
            from trajectory import mark_capture_paused_in_tx
            await mark_capture_paused_in_tx(db, user_id, session_id)

    if not synthetic:
        from session.status import discard_pending_abort
        discard_pending_abort(session_id)
    runtime.publish_invalidated(invalidated_questions)
    from bus.events import MESSAGE_CREATED
    from models.message import id_to_iso
    msg = MessageWithParts(
        id=msg_id,
        session_id=session_id,
        role="user",
        parts=[text_part],
        created_at=id_to_iso(msg_id),
        client_message_id=client_message_id,
        agent=agent,
        model=model,
    )
    log.info(f"PUBLISHING MESSAGE_CREATED for {msg_id} userId={user_id}")
    bus.publish(MESSAGE_CREATED, {
        "userId": user_id,
        "sessionId": session_id,
        "message": msg.model_dump(),
    })

    return msg


async def create_assistant_message(
    session_id: str,
    parent_id: str,
    model_id: str | None = None,
    agent: str | None = None,
    user_id: str = "default",
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

    from question import runtime
    async with runtime.transaction(session_id, user_id) as (db, _, _execution):
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
    bus.publish(MESSAGE_CREATED, {
        "userId": user_id,
        "sessionId": session_id,
        "message": msg.model_dump(),
    })

    return info


async def update_message_info(info: MessageInfo, user_id: str = "default") -> None:
    """Update a message's info in the database."""
    values: dict = {}
    if info.tokens:
        values["tokens"] = info.tokens.model_dump()
    if info.finish is not None:
        values["finish"] = info.finish
    if info.error is not None:
        values["error"] = info.error
    if info.model_id is not None:
        values["model_id"] = info.model_id
    if info.summary is not None:
        values["summary"] = info.summary
    if info.structured is not None:
        values["structured"] = info.structured

    if values:
        from question import runtime
        async with runtime.transaction(info.session_id, user_id) as (db, _, _execution):
            await db.execute(
                update(MessageORM).where(MessageORM.id == info.id).values(**values)
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
    bus.publish(MESSAGE_UPDATED, {
        "userId": user_id,
        "sessionId": info.session_id,
        "message": msg_update,
    })


async def save_part(part: MessagePart, is_new: bool = False, *, user_id: str) -> None:
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
    part_dict = public_part_data(part.model_dump())
    msg_id = part_dict.get("message_id", "")
    session_id = part_dict.get("session_id", "")

    # PostgreSQL JSONB cannot store \u0000 (null bytes). Strip them to avoid
    # "unsupported Unicode escape sequence" errors from asyncpg.
    import json as _json
    part_dict = _json.loads(_json.dumps(part_dict).replace("\\u0000", ""))

    from question import runtime
    prepared_assets = (await prepare_trajectory_assets(session_id, user_id, [part_dict["asset_id"]])
        if part_dict.get("type") == "file" and part_dict.get("asset_id") else {})
    async with runtime.transaction(session_id, user_id) as (db, _, _execution):
        if is_new:
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
        else:
            values = {"data": part_dict, **identity_values}
            await db.execute(
                update(PartORM).where(PartORM.id == part.id).values(**values)
            )
        await record_projection_in_tx(db, session_id, user_id, "part.committed",
            {"part": part_dict, "operation": "created" if is_new else "updated"},
            message_id=msg_id, part_id=part.id, prepared_assets=prepared_assets)

    # Exclude internal fields from SSE event (frontend doesn't need them)
    sse_dict = public_part_data(part.model_dump(exclude={
        "session_id",
        "message_id",
        "state",
        *PRIVATE_TOOL_PART_FIELDS,
    }))

    from bus.events import PART_CREATED, PART_UPDATED
    event_type = PART_CREATED if is_new else PART_UPDATED
    bus.publish(event_type, {
        "userId": user_id,
        "sessionId": session_id,
        "messageId": msg_id,
        "part": sse_dict,
    })


async def get_messages(session_id: str, offset: int = 0, limit: int = 200, user_id: str | None = None) -> list[MessageWithParts]:
    """Get messages for a session with their parts in a single query.

    Args:
        user_id: If provided, verifies session belongs to this user (defense-in-depth).
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

        # Get messages with pagination
        msg_result = await db.execute(
            select(MessageORM).where(MessageORM.session_id == session_id)
            .order_by(MessageORM.created_at)
            .offset(offset).limit(limit)
        )
        messages = msg_result.scalars().all()

        if not messages:
            return []

        msg_ids = [m.id for m in messages]

        # Get all parts for these messages in one query
        parts_result = await db.execute(
            select(PartORM).where(PartORM.message_id.in_(msg_ids))
            .order_by(PartORM.created_at)
        )
        all_parts = parts_result.scalars().all()

    # Group parts by message_id
    parts_by_msg: dict[str, list[dict]] = {}
    for p in all_parts:
        parts_by_msg.setdefault(p.message_id, []).append(public_part_data(p.data))

    # Build result
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
            model=m.model,
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


async def delete_messages_from(
    session_id: str,
    message_id: str,
    *,
    user_id: str = "default",
) -> str | None:
    """Drop `message_id` and everything after it. Returns the id of the last
    user message that survives, or None if the session has none left.

    This is what "regenerate" is made of: the assistant's attempt is removed so
    the loop answers the same prompt again, rather than the model reading its
    own failed turn as context and apologising for it.

    A hard delete, deliberately. The alternative — a `superseded` flag — leaves
    the old turn in every history read (compaction, token counting, the model's
    own context) unless all of them learn to filter it, and one that forgets is
    a silent context bug rather than a visible one.
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
                    MessageORM.created_at >= cutoff,
                ).order_by(MessageORM.id)
            )).scalars().all())

            if doomed:
                from question import runtime
                execution = await runtime.execution_locked(db, session_id, user_id)
                invalidated_questions = await runtime.invalidate_locked(db, execution)
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
                ).order_by(MessageORM.created_at.desc()).limit(1)
            )
            last_user = survivor.scalar_one_or_none()

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
    "dismiss a failure", not a general history-rewriting endpoint. Returns the
    number of messages removed, 0 if the id does not qualify.
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

            await delete_internal_parts_for_messages_locked(db, session_row, doomed)
            await db.execute(PartORM.__table__.delete().where(PartORM.message_id.in_(doomed)))
            await db.execute(MessageORM.__table__.delete().where(MessageORM.id.in_(doomed)))
            await record_projection_in_tx(db, session_id, user_id, "history.reverted",
                {"reason": "dismiss_failed_turn", "removed_message_ids": doomed})

    log.info(f"Dismissed failed turn {message_id} in {session_id} ({len(doomed)} message(s))")
    return len(doomed)


async def set_message_reaction(message_id: str, session_id: str, reaction: str | None) -> None:
    """Persist thumbs up/down feedback for an assistant message."""
    async with get_db_session() as db:
        await db.execute(
            update(MessageORM)
            .where(MessageORM.id == message_id, MessageORM.session_id == session_id)
            .values(reaction=reaction)
        )


async def get_parts_for_message(message_id: str) -> list[dict]:
    """Get all parts for a specific message, ordered by creation time."""
    async with get_db_session() as db:
        result = await db.execute(
            select(PartORM).where(PartORM.message_id == message_id)
            .order_by(PartORM.created_at)
        )
        return [public_part_data(p.data) for p in result.scalars().all()]


async def update_part_data(part_id: str, data: dict, publish: bool = False, user_id: str = "default") -> None:
    """Update a part's data directly by ID.

    Persists silently by default — some callers (compaction pruning) only
    stamp bookkeeping the UI never renders. Pass ``publish=True`` when the
    change is user-visible, e.g. a tool flipping to ``error`` on abort: without
    the event the row spins forever, because the store still holds the stale
    ``running`` copy until something forces a refetch.
    """
    data = public_part_data(data)
    async with get_db_session() as db:
        row = await db.get(PartORM, part_id)
        if row is None:
            return
        if row.user_id != user_id:
            raise ValueError("Part does not belong to the execution owner")
        session_id, message_id = row.session_id, row.message_id
    prepared_assets = (await prepare_trajectory_assets(session_id, user_id, [data["asset_id"]])
        if data.get("type") == "file" and data.get("asset_id") else {})
    from question import runtime
    async with runtime.transaction(session_id, user_id) as (db, _, _execution):
        row = await db.get(PartORM, part_id)
        if row is None:
            return
        await db.execute(
            update(PartORM).where(PartORM.id == part_id, PartORM.user_id == user_id,
                                 PartORM.session_id == session_id).values(data=data)
        )
        await record_projection_in_tx(db, session_id, user_id, "part.committed",
            {"part": data, "operation": "updated"}, message_id=message_id, part_id=part_id,
            prepared_assets=prepared_assets)
    if not publish:
        return
    sse_dict = {k: v for k, v in data.items() if k not in ("session_id", "message_id", "state")}
    from bus.events import PART_UPDATED
    bus.publish(PART_UPDATED, {
        "userId": user_id,
        "sessionId": session_id,
        "messageId": message_id,
        "part": sse_dict,
    })


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
