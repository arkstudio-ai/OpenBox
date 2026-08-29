"""Durable NeedsAgent continuation dispatcher.

Only this component bridges the SkillJob and Agent state machines. It claims a
pending inbox row when its source session is idle, injects one idempotent
synthetic turn, and writes the resulting assistant text back as an
``agent_result`` input. Ordinary job completion never starts an LLM turn.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select, update

from core.log import create_logger
from db.base import get_db_session
from db.models.session import Session
from db.models.session_inbox import SessionInbox
from db.models.skill_job import SkillJob
from skill_runtime import repository as repo
from skill_runtime.types import DesiredState, JobStatus

log = create_logger("skill_runtime.inbox")

POLL_SECONDS = 2.0
CLAIM_STALE_SECONDS = 30 * 60
MAX_CONCURRENCY = 2
RECOVERY_SCAN_SECONDS = 60.0
CLAIM_HEARTBEAT_SECONDS = 60.0
ACTIVE_AGENT_STATUSES = ("busy", "compacting")
RECOVERABLE_AGENT_STATUSES = (*ACTIVE_AGENT_STATUSES, "error")
CANDIDATE_SCAN_LIMIT = 64


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _recover_abandoned_claims() -> None:
    """Re-open dispatcher claims whose DB heartbeat stopped.

    ``consumed_at`` is the claim heartbeat while status=processing. A live
    long-running Agent turn keeps it fresh; after a hard process exit a later
    scan can safely recover the row even if the Session was left ``busy``.
    """
    cutoff = _utcnow() - timedelta(seconds=CLAIM_STALE_SECONDS)
    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(
                    SessionInbox.id,
                    SessionInbox.session_id,
                    SessionInbox.user_id,
                    SessionInbox.consumed_at,
                    SessionInbox.claim_token,
                    SessionInbox.payload,
                )
                .where(
                    SessionInbox.status == "processing",
                    SessionInbox.consumed_at.isnot(None),
                    SessionInbox.consumed_at < cutoff,
                )
                .limit(20)
            )
        ).all()
    for (
        inbox_id,
        session_id,
        user_id,
        claim_heartbeat,
        claim_token,
        payload,
    ) in rows:
        timestamp_released = False
        async with get_db_session() as db:
            recovered = await db.execute(
                update(SessionInbox)
                .where(
                    SessionInbox.id == inbox_id,
                    SessionInbox.status == "processing",
                    SessionInbox.consumed_at < cutoff,
                    SessionInbox.claim_token == claim_token,
                )
                .values(status="pending", consumed_at=None, claim_token=None)
            )
            if recovered.rowcount != 1:
                continue
            released = await db.execute(
                update(Session)
                .where(
                    Session.id == session_id,
                    Session.user_id == user_id,
                    Session.status.in_(RECOVERABLE_AGENT_STATUSES),
                    # A user may have explicitly stopped the abandoned turn and
                    # started newer work in the same session. Do not let recovery
                    # reset that newer run's status.
                    Session.updated_at <= claim_heartbeat,
                )
                .values(status="idle", updated_at=_utcnow())
            )
            timestamp_released = released.rowcount == 1

        if timestamp_released:
            continue

        # run_loop updates Session.updated_at after the inbox reservation. If
        # the process dies before its next inbox heartbeat, the timestamp guard
        # above deliberately refuses to release the Session, but leaving it
        # busy would also make the recovered pending row permanently
        # unclaimable. A durable synthetic marker gives us a stronger fence:
        # release only when no later real user turn superseded that marker, and
        # repeat the latest-user check inside the UPDATE to close the race.
        marker = _marker_from_values(inbox_id, payload)
        try:
            _, superseded, marker_exists, tail_user_id = await _continuation_view(
                session_id, user_id, marker
            )
        except Exception as exc:
            log.warning(
                f"Could not inspect abandoned continuation {inbox_id}; "
                f"session remains fenced: {type(exc).__name__}"
            )
            continue
        if not marker_exists or superseded or not tail_user_id:
            continue
        from db.models.message import Message

        async with get_db_session() as db:
            latest_user_id = (
                select(Message.id)
                .where(
                    Message.session_id == session_id,
                    Message.user_id == user_id,
                    Message.role == "user",
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(1)
                .scalar_subquery()
            )
            still_unclaimed = (
                select(SessionInbox.id)
                .where(
                    SessionInbox.id == inbox_id,
                    SessionInbox.status == "pending",
                    SessionInbox.claim_token.is_(None),
                )
                .exists()
            )
            await db.execute(
                update(Session)
                .where(
                    Session.id == session_id,
                    Session.user_id == user_id,
                    Session.status.in_(RECOVERABLE_AGENT_STATUSES),
                    latest_user_id == tail_user_id,
                    # Marker inspection happens after the stale claim was
                    # reopened. A new replica may have claimed it meanwhile;
                    # never clear that replica's freshly reserved Session.
                    still_unclaimed,
                )
                .values(status="idle", updated_at=_utcnow())
            )


async def _expire_obsolete() -> None:
    """Drop continuations whose source job no longer waits for Agent work."""
    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(SessionInbox.id)
                .outerjoin(SkillJob, SkillJob.id == SessionInbox.source_job_id)
                .where(
                    SessionInbox.status == "pending",
                    (
                        SkillJob.id.is_(None)
                        | (SkillJob.user_id != SessionInbox.user_id)
                        | ~_source_job_still_justifies()
                    ),
                )
                .limit(50)
            )
        ).scalars().all()
        if rows:
            await db.execute(
                update(SessionInbox)
                .where(SessionInbox.id.in_(rows), SessionInbox.status == "pending")
                .values(status="expired", consumed_at=_utcnow())
            )


async def _expire_unroutable() -> None:
    """Cancel continuations whose Session no longer provides a valid route.

    Keep a pending row as the durable retry token until cancellation succeeds.
    The unavailable Session makes it ineligible for a claim, so this does not
    risk starting another Agent turn. Processing rows are demoted first to
    revoke their fencing token when a Session disappears mid-turn.
    """
    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(SessionInbox.id, SessionInbox.source_job_id, SessionInbox.user_id)
                .outerjoin(Session, Session.id == SessionInbox.session_id)
                .where(
                    SessionInbox.status.in_(("pending", "processing")),
                    (Session.id.is_(None))
                    | (Session.user_id != SessionInbox.user_id)
                    | (Session.is_deleted == True),  # noqa: E712
                )
                .limit(50)
            )
        ).all()
        if rows:
            await db.execute(
                update(SessionInbox)
                .where(
                    SessionInbox.id.in_([row[0] for row in rows]),
                    SessionInbox.status.in_(("pending", "processing")),
                )
                .values(status="pending", consumed_at=None, claim_token=None)
            )
    for inbox_id, job_id, user_id in rows:
        try:
            await repo.request_cancel(job_id, user_id, reason="continuation_session_unavailable")
        except repo.JobNotFound:
            pass
        except Exception as exc:
            # Leave it pending. The next dispatcher pass retries the durable
            # cancellation instead of losing it behind an expired inbox row.
            log.warning(
                f"Could not cancel unroutable continuation {inbox_id}: "
                f"{type(exc).__name__}"
            )
            continue
        async with get_db_session() as db:
            await db.execute(
                update(SessionInbox)
                .where(
                    SessionInbox.id == inbox_id,
                    SessionInbox.source_job_id == job_id,
                    SessionInbox.user_id == user_id,
                    SessionInbox.status == "pending",
                )
                .values(status="expired", consumed_at=_utcnow(), claim_token=None)
            )


async def _claim_candidates(per_user_limit: int) -> list[tuple[str, str, str]]:
    """Return a small tenant-fair window of eligible continuation rows.

    The count filter is only a cheap prefilter. ``_try_claim`` repeats it while
    holding a PostgreSQL per-user transaction lock, which is the actual quota
    boundary across API replicas.
    """
    async with get_db_session() as db:
        active = (
            select(
                Session.user_id.label("active_user_id"),
                func.count().label("active_count"),
            )
            .where(Session.status.in_(ACTIVE_AGENT_STATUSES))
            .where(Session.is_deleted == False)  # noqa: E712
            .group_by(Session.user_id)
            .subquery()
        )
        eligible = (
            select(
                SessionInbox.id.label("inbox_id"),
                SessionInbox.session_id.label("session_id"),
                SessionInbox.user_id.label("user_id"),
                SessionInbox.created_at.label("created_at"),
                func.row_number()
                .over(
                    partition_by=SessionInbox.user_id,
                    order_by=SessionInbox.created_at.asc(),
                )
                .label("tenant_rank"),
            )
            .join(Session, Session.id == SessionInbox.session_id)
            .join(SkillJob, SkillJob.id == SessionInbox.source_job_id)
            .outerjoin(active, active.c.active_user_id == SessionInbox.user_id)
            .where(
                SessionInbox.status == "pending",
                SessionInbox.user_id == Session.user_id,
                Session.status == "idle",
                Session.is_deleted == False,  # noqa: E712
                SkillJob.user_id == SessionInbox.user_id,
                _source_job_still_justifies(),
            )
        )
        if per_user_limit > 0:
            eligible = eligible.where(
                func.coalesce(active.c.active_count, 0) < per_user_limit
            )
        ranked = eligible.subquery()
        return [
            (row.inbox_id, row.session_id, row.user_id)
            for row in (
                await db.execute(
                    select(ranked.c.inbox_id, ranked.c.session_id, ranked.c.user_id)
                    .order_by(ranked.c.tenant_rank.asc(), ranked.c.created_at.asc())
                    .limit(CANDIDATE_SCAN_LIMIT)
                )
            ).all()
        ]


async def _try_claim(
    inbox_id: str,
    session_id: str,
    user_id: str,
    *,
    per_user_limit: int,
) -> SessionInbox | None:
    now = _utcnow()
    claim_token = uuid.uuid4().hex
    async with get_db_session() as db:
        if per_user_limit > 0:
            # Serialize count + reservation for a tenant across dispatcher
            # replicas. SQLite is the documented single-process development
            # path and relies on the conditional row updates below.
            if db.get_bind().dialect.name == "postgresql":
                await db.execute(
                    select(func.pg_advisory_xact_lock(func.hashtextextended(user_id, 0)))
                )
            active_count = (
                await db.execute(
                    select(func.count()).select_from(Session).where(
                        Session.user_id == user_id,
                        Session.status.in_(ACTIVE_AGENT_STATUSES),
                        Session.is_deleted == False,  # noqa: E712
                    )
                )
            ).scalar_one()
            if active_count >= per_user_limit:
                return None

        # Recheck the complete tenant/source-state predicate inside the claim
        # transaction; the candidate window is deliberately only advisory.
        candidate = (
            await db.execute(
                select(SessionInbox.id)
                .join(Session, Session.id == SessionInbox.session_id)
                .join(SkillJob, SkillJob.id == SessionInbox.source_job_id)
                .where(
                    SessionInbox.id == inbox_id,
                    SessionInbox.session_id == session_id,
                    SessionInbox.user_id == user_id,
                    SessionInbox.status == "pending",
                    SessionInbox.user_id == Session.user_id,
                    Session.id == session_id,
                    Session.status == "idle",
                    Session.is_deleted == False,  # noqa: E712
                    SkillJob.user_id == SessionInbox.user_id,
                    _source_job_still_justifies(),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if candidate is None:
            return None

        reserved = await db.execute(
            update(Session)
            .where(
                Session.id == session_id,
                Session.user_id == user_id,
                Session.status == "idle",
                Session.is_deleted == False,  # noqa: E712
            )
            .values(status="busy", updated_at=now)
        )
        if reserved.rowcount != 1:
            return None
        claimed = await db.execute(
            update(SessionInbox)
            .where(
                SessionInbox.id == inbox_id,
                SessionInbox.user_id == user_id,
                SessionInbox.status == "pending",
            )
            .values(status="processing", consumed_at=now, claim_token=claim_token)
        )
        if claimed.rowcount != 1:
            await db.execute(
                update(Session)
                .where(
                    Session.id == session_id,
                    Session.user_id == user_id,
                    Session.status == "busy",
                    # Release only the reservation made above. This guard is
                    # intentionally redundant inside today's transaction: it
                    # prevents a future refactor from turning a failed inbox
                    # claim into a cross-run/session-status clobber.
                    Session.updated_at == now,
                )
                .values(status="idle", updated_at=now)
            )
            return None
        return (
            await db.execute(
                select(SessionInbox).where(
                    SessionInbox.id == inbox_id,
                    SessionInbox.user_id == user_id,
                )
            )
        ).scalar_one()


async def _claim_one(per_user_limit: int = 0) -> SessionInbox | None:
    for inbox_id, session_id, user_id in await _claim_candidates(per_user_limit):
        claimed = await _try_claim(
            inbox_id,
            session_id,
            user_id,
            per_user_limit=per_user_limit,
        )
        if claimed is not None:
            return claimed
    return None


COMPLETED_KIND = "job_completed"


def _source_job_still_justifies():
    """SQL predicate: does the source job still warrant this continuation?

    The two kinds have opposite requirements, which is why this cannot be one
    status check. A ``job_needs_agent`` row is only valid while its job is
    parked in waiting_agent — if the job moved on, the answer is moot. A
    ``job_completed`` row is only valid *because* its job finished; requiring
    waiting_agent there expired every notice the instant it was written.
    """
    return or_(
        and_(
            SessionInbox.kind != COMPLETED_KIND,
            SkillJob.status == JobStatus.WAITING_AGENT.value,
            SkillJob.desired_state == DesiredState.RUN.value,
        ),
        and_(
            SessionInbox.kind == COMPLETED_KIND,
            SkillJob.status == JobStatus.SUCCEEDED.value,
        ),
    )


def _source_state_ok(kind: str | None, status: str | None, desired: str | None) -> bool:
    """The same rule in Python, for the pre-write-back recheck."""
    if (kind or "") == COMPLETED_KIND:
        return status == JobStatus.SUCCEEDED.value
    return status == JobStatus.WAITING_AGENT.value and desired == DesiredState.RUN.value


def _is_write_back_kind(item: SessionInbox) -> bool:
    """Does this continuation owe its source job an ``agent_result``?

    ``job_needs_agent`` does: the job is parked in waiting_agent and resumes on
    the answer. ``job_completed`` does not: the job is already terminal, and
    writing an input into a settled job would be rejected — the turn exists
    only so the workflow carries on.
    """
    return (item.kind or "") != "job_completed"


def _completion_prompt(item: SessionInbox) -> str:
    payload = json.dumps(item.payload or {}, ensure_ascii=False, sort_keys=True)
    return (
        "[后台作业已完成，请继续推进流程]\n"
        f"job_id={item.source_job_id}\n"
        f"inbox_id={item.id}\n"
        f"result={payload}\n\n"
        "这是平台的完成通知，不是用户发言。请据此推进到下一步："
        "该启动下一个作业就启动，该等用户确认就明确说明在等什么，"
        "没有下一步就简短收尾。不要重复已完成的这一步，"
        "也不要轮询后台作业。"
    )


def _continuation_prompt(item: SessionInbox) -> str:
    # Dispatcher bookkeeping is not handler context and must not leak into the
    # model prompt or become part of its domain answer.
    public_payload = {
        key: value
        for key, value in (item.payload or {}).items()
        if not str(key).startswith("_dispatch_")
    }
    payload = json.dumps(public_payload, ensure_ascii=False, sort_keys=True)
    return (
        "[后台作业请求一次 Agent 续作]\n"
        f"job_id={item.source_job_id}\n"
        f"inbox_id={item.id}\n"
        f"context={payload}\n\n"
        "请只完成这一次有界续作并给出可供后台 handler 继续执行的结论。"
        "不要轮询后台作业；你的最终文本会由平台作为 agent_result 原子写回。"
    )


async def _consume_claim(item: SessionInbox, *, marker_is_present: bool, tail_user_id) -> None:
    """Retire a processed claim and release the session it reserved."""
    async with get_db_session() as db:
        finished_claim = await db.execute(
            update(SessionInbox)
            .where(
                SessionInbox.id == item.id,
                SessionInbox.status == "processing",
                SessionInbox.claim_token == item.claim_token,
            )
            .values(status="consumed", consumed_at=_utcnow(), claim_token=None)
        )
        if finished_claim.rowcount == 1:
            await _release_reserved_session(
                db,
                item,
                marker_is_present=marker_is_present,
                tail_user_id=tail_user_id,
            )


async def _heartbeat_claim(item: SessionInbox, stop: asyncio.Event) -> None:
    """Keep a processing claim distinguishable from an abandoned one."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=CLAIM_HEARTBEAT_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        try:
            async with get_db_session() as db:
                result = await db.execute(
                    update(SessionInbox)
                    .where(
                        SessionInbox.id == item.id,
                        SessionInbox.status == "processing",
                        SessionInbox.claim_token == item.claim_token,
                    )
                    .values(consumed_at=_utcnow())
                )
        except Exception as exc:
            # Retry while the Agent turn is live. Recovery uses a 30-minute
            # stale window, so short database incidents cannot steal the claim.
            log.warning(
                f"NeedsAgent claim heartbeat {item.id} failed: "
                f"{type(exc).__name__}"
            )
            continue
        if result.rowcount != 1:
            return


async def _release_reserved_session(
    db,
    item: SessionInbox,
    *,
    marker_is_present: bool,
    tail_user_id: str | None = None,
) -> None:
    """Release only the Session reservation still owned by this inbox turn."""
    from db.models.message import Message

    guards = [
        Session.id == item.session_id,
        Session.user_id == item.user_id,
        Session.status.in_(("busy", "compacting", "error")),
    ]
    if marker_is_present and tail_user_id:
        latest_user_id = (
            select(Message.id)
            .where(
                Message.session_id == item.session_id,
                Message.user_id == item.user_id,
                Message.role == "user",
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        # The Python-side view has established that this tail contains only
        # the continuation and its compaction messages. The SQL id fence closes
        # the remaining race with a human message arriving before this UPDATE.
        guards.append(latest_user_id == tail_user_id)
    else:
        guards.append(Session.updated_at <= item.consumed_at)
    await db.execute(
        update(Session)
        .where(*guards)
        .values(status="idle", updated_at=_utcnow())
    )


def _message_role(message) -> str:
    role = message.role if isinstance(message.role, str) else message.role.value
    return str(role)


def _is_compaction_user(message) -> bool:
    """Compaction messages are internal to the same Agent turn, not a user
    instruction that supersedes a NeedsAgent continuation."""
    if getattr(message, "agent", None) == "compaction":
        return True
    for part in message.parts or []:
        data = part if isinstance(part, dict) else part.model_dump()
        if (
            data.get("type") == "text"
            and data.get("synthetic") is True
            and str(data.get("text") or "").startswith("Context was compacted.")
        ):
            return True
    return False


def _is_continuation_marker(message, marker: str) -> bool:
    """Recognize only the platform's synthetic user marker.

    A matching client_message_id alone is not enough: historical data may
    predate the reserved-prefix API guard. Treating an ordinary user message as
    the marker would let its following assistant text become an agent_result.
    """
    if (
        getattr(message, "client_message_id", None) != marker
        or _message_role(message) != "user"
    ):
        return False
    for part in message.parts or []:
        data = part if isinstance(part, dict) else part.model_dump()
        if data.get("type") == "text" and data.get("synthetic") is True:
            return True
    return False


def _marker_from_values(inbox_id: str, payload: dict | None) -> str:
    generation = int((payload or {}).get("_dispatch_marker_generation") or 0)
    suffix = f":{generation}" if generation else ""
    return f"sji:{inbox_id}{suffix}"


def _marker_for(item: SessionInbox) -> str:
    return _marker_from_values(item.id, item.payload)


async def _advance_marker(item: SessionInbox) -> str:
    """Start a fresh synthetic turn after a human superseded an incomplete one."""
    payload = dict(item.payload or {})
    generation = int(payload.get("_dispatch_marker_generation") or 0) + 1
    payload["_dispatch_marker_generation"] = generation
    async with get_db_session() as db:
        result = await db.execute(
            update(SessionInbox)
            .where(
                SessionInbox.id == item.id,
                SessionInbox.status == "processing",
                SessionInbox.claim_token == item.claim_token,
            )
            .values(payload=payload)
        )
        if result.rowcount != 1:
            raise RuntimeError("NeedsAgent inbox claim was lost while advancing its marker")
    item.payload = payload
    return _marker_for(item)


async def _claim_is_live(item: SessionInbox) -> bool:
    async with get_db_session() as db:
        owned = (
            await db.execute(
                select(SessionInbox.id).where(
                    SessionInbox.id == item.id,
                    SessionInbox.status == "processing",
                    SessionInbox.claim_token == item.claim_token,
                )
            )
        ).scalar_one_or_none()
    return owned is not None


async def _continuation_view(
    session_id: str,
    user_id: str,
    marker: str,
) -> tuple[str, bool, bool, str | None]:
    """Inspect one synthetic turn without crossing into a newer user turn.

    Tool-step commentary may already contain text when a process dies. Treating
    that partial message as an agent_result would skip the remaining tools and
    make the source job continue from an invented completion. A normal user
    message after the marker is a hard boundary; compaction's internal user
    messages remain part of this same turn.

    Returns ``(final_text, superseded, marker_exists, latest_user_message_id)``.
    """
    from session.session import get_messages

    messages = await get_messages(session_id, user_id=user_id)
    marker_index = next(
        (
            index
            for index, message in enumerate(messages)
            if _is_continuation_marker(message, marker)
        ),
        -1,
    )
    latest_user_id = next(
        (message.id for message in reversed(messages) if _message_role(message) == "user"),
        None,
    )
    if marker_index < 0:
        return "", False, False, latest_user_id

    end = len(messages)
    superseded = False
    for index, message in enumerate(messages[marker_index + 1 :], start=marker_index + 1):
        if _message_role(message) == "user" and not _is_compaction_user(message):
            end = index
            superseded = True
            break

    for message in reversed(messages[marker_index + 1 : end]):
        if (
            _message_role(message) != "assistant"
            or getattr(message, "finish", None) != "stop"
            or getattr(message, "summary", False)
            or getattr(message, "agent", None) == "compaction"
        ):
            continue
        for part in reversed(message.parts or []):
            data = part if isinstance(part, dict) else part.model_dump()
            if data.get("type") == "text" and str(data.get("text") or "").strip():
                return str(data["text"]).strip(), superseded, True, latest_user_id
    return "", superseded, True, latest_user_id


async def _refresh_release_fence(
    item: SessionInbox,
    marker: str,
    tail_user_id: str | None,
) -> tuple[bool, str | None]:
    """Refresh the message-tail fence after an Agent run or error path."""
    try:
        _, superseded, exists, latest_user_id = await _continuation_view(
            item.session_id, item.user_id, marker
        )
        return exists and not superseded, latest_user_id
    except Exception as exc:
        # Falling back to the reservation timestamp is fail-closed after a run:
        # run_loop updates Session.updated_at, so it cannot release a newer run.
        log.warning(
            f"Could not refresh continuation tail fence for {item.id}: "
            f"{type(exc).__name__}"
        )
        return False, tail_user_id


async def _run_claim(item: SessionInbox) -> None:
    marker = _marker_for(item)
    marker_is_present = False
    tail_user_id: str | None = None
    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat_claim(item, heartbeat_stop))
    try:
        from session.session import create_user_message

        if not await _claim_is_live(item):
            return
        async with get_db_session() as db:
            source_state = (
                await db.execute(
                    select(SkillJob.status, SkillJob.desired_state).where(
                        SkillJob.id == item.source_job_id,
                        SkillJob.user_id == item.user_id,
                    )
                )
            ).one_or_none()
            if source_state is None or not _source_state_ok(
                item.kind, source_state[0], source_state[1]
            ):
                expired_claim = await db.execute(
                    update(SessionInbox)
                    .where(
                        SessionInbox.id == item.id,
                        SessionInbox.status == "processing",
                        SessionInbox.claim_token == item.claim_token,
                    )
                    .values(status="expired", consumed_at=_utcnow(), claim_token=None)
                )
                if expired_claim.rowcount == 1:
                    await db.execute(
                        update(Session)
                        .where(
                            Session.id == item.session_id,
                            Session.user_id == item.user_id,
                            Session.status == "busy",
                            # The claim and the Session reservation share one
                            # timestamp. If a newer run has touched this session,
                            # its status belongs to that run and must not be reset.
                            Session.updated_at <= item.consumed_at,
                        )
                        .values(status="idle", updated_at=_utcnow())
                    )
                return
        result_text, superseded, marker_exists, tail_user_id = await _continuation_view(
            item.session_id, item.user_id, marker
        )
        if not result_text:
            if marker_exists and superseded:
                marker = await _advance_marker(item)
                marker_exists = False
            if not marker_exists:
                await create_user_message(
                    session_id=item.session_id,
                    text=(
                        _continuation_prompt(item)
                        if _is_write_back_kind(item)
                        else _completion_prompt(item)
                    ),
                    synthetic=True,
                    client_message_id=marker,
                    user_id=item.user_id,
                )
            marker_is_present = True
            result_text, _, _, tail_user_id = await _continuation_view(
                item.session_id, item.user_id, marker
            )
        else:
            # Reusing a completed turn is safe. If a later human turn exists,
            # release this claim via its reservation timestamp instead of
            # pretending the old marker still owns the session tail.
            marker_is_present = not superseded

        if not result_text:
            from agent.loop import run_loop

            if not await _claim_is_live(item):
                return
            loop_result = await run_loop(item.session_id, user_id=item.user_id)
            async with get_db_session() as db:
                session_status = (
                    await db.execute(
                        select(Session.status).where(
                            Session.id == item.session_id,
                            Session.user_id == item.user_id,
                        )
                    )
                ).scalar_one_or_none()
            if loop_result is None or session_status == "error":
                raise RuntimeError("continuation agent turn did not produce a successful result")
            result_text, superseded, _, tail_user_id = await _continuation_view(
                item.session_id, item.user_id, marker
            )
            marker_is_present = not superseded
        # If a prior process committed an assistant stop but died before the
        # agent_result input, the lookup above reuses that durable final answer;
        # re-running its tools would violate at-most-once intent.
        if not _is_write_back_kind(item):
            # Terminal job: nothing to resume, so nothing to verify. Requiring
            # assistant text here is the NeedsAgent contract — the answer *is*
            # the deliverable there. A completion notice only has to reach the
            # session; whether the turn replied in prose or went straight to
            # starting the next job, the notice has done its job.
            if not await _claim_is_live(item):
                return
            await _consume_claim(
                item, marker_is_present=marker_is_present, tail_user_id=tail_user_id
            )
            return
        if not result_text:
            raise RuntimeError("continuation agent turn produced no assistant text")
        if not await _claim_is_live(item):
            return
        await repo.add_input(
            item.source_job_id,
            item.user_id,
            kind="agent_result",
            payload={"text": result_text, "inbox_id": item.id},
            idempotency_key=f"inbox:{item.id}",
            source_event_id=str(item.source_event_seq),
        )
        await _consume_claim(
            item, marker_is_present=marker_is_present, tail_user_id=tail_user_id
        )
    except asyncio.CancelledError:
        # Graceful API shutdown must not strand a 30-minute processing lease.
        # The synthetic marker/final text are durable, so a new dispatcher can
        # safely resume or reuse them after this claim is relinquished.
        marker_is_present, tail_user_id = await _refresh_release_fence(
            item, marker, tail_user_id
        )
        async with get_db_session() as db:
            relinquished = await db.execute(
                update(SessionInbox)
                .where(
                    SessionInbox.id == item.id,
                    SessionInbox.status == "processing",
                    SessionInbox.claim_token == item.claim_token,
                )
                .values(status="pending", consumed_at=None, claim_token=None)
            )
            if relinquished.rowcount == 1:
                await _release_reserved_session(
                    db,
                    item,
                    marker_is_present=marker_is_present,
                    tail_user_id=tail_user_id,
                )
        raise
    except repo.InputNotAllowed:
        # The job ended while the continuation ran; consuming the inbox is the
        # idempotent convergence, not an error worth another Agent turn.
        marker_is_present, tail_user_id = await _refresh_release_fence(
            item, marker, tail_user_id
        )
        async with get_db_session() as db:
            finished_claim = await db.execute(
                update(SessionInbox)
                .where(
                    SessionInbox.id == item.id,
                    SessionInbox.status == "processing",
                    SessionInbox.claim_token == item.claim_token,
                )
                .values(status="consumed", consumed_at=_utcnow(), claim_token=None)
            )
            if finished_claim.rowcount == 1:
                await _release_reserved_session(
                    db,
                    item,
                    marker_is_present=marker_is_present,
                    tail_user_id=tail_user_id,
                )
    except Exception as exc:
        log.warning(
            f"NeedsAgent continuation {item.id} failed: {type(exc).__name__}"
        )
        marker_is_present, tail_user_id = await _refresh_release_fence(
            item, marker, tail_user_id
        )
        payload = dict(item.payload or {})
        failures = int(payload.get("_dispatch_failures") or 0) + 1
        payload["_dispatch_failures"] = failures
        exhausted = failures >= 3
        claim_transitioned = False
        async with get_db_session() as db:
            transitioned = await db.execute(
                update(SessionInbox)
                .where(
                    SessionInbox.id == item.id,
                    SessionInbox.status == "processing",
                    SessionInbox.claim_token == item.claim_token,
                )
                .values(
                    status="expired" if exhausted else "pending",
                    consumed_at=_utcnow() if exhausted else None,
                    payload=payload,
                    claim_token=None,
                )
            )
            # There is no Session run token yet. The helper fences cleanup with
            # either the synthetic marker or the original reservation timestamp,
            # so even a cached completed turn can be released without clobbering
            # a newer human turn.
            claim_transitioned = transitioned.rowcount == 1
            if claim_transitioned:
                await _release_reserved_session(
                    db,
                    item,
                    marker_is_present=marker_is_present,
                    tail_user_id=tail_user_id,
                )
        if exhausted and claim_transitioned:
            try:
                await repo.request_cancel(
                    item.source_job_id,
                    item.user_id,
                    reason="agent_continuation_failed",
                )
            except repo.JobNotFound:
                pass
    finally:
        heartbeat_stop.set()
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


class InboxDispatcher:
    def __init__(
        self,
        poll_seconds: float = POLL_SECONDS,
        *,
        per_user_limit: int = 0,
    ):
        self.poll_seconds = poll_seconds
        self.per_user_limit = max(0, per_user_limit)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._inflight: set[asyncio.Task] = set()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        if self._inflight:
            _, pending = await asyncio.wait(self._inflight, timeout=30)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _run(self) -> None:
        next_recovery = 0.0
        while not self._stop.is_set():
            try:
                now = asyncio.get_running_loop().time()
                if now >= next_recovery:
                    await _recover_abandoned_claims()
                    next_recovery = now + RECOVERY_SCAN_SECONDS
                await _expire_unroutable()
                await _expire_obsolete()
                self._inflight = {task for task in self._inflight if not task.done()}
                while len(self._inflight) < MAX_CONCURRENCY:
                    item = await _claim_one(self.per_user_limit)
                    if item is None:
                        break
                    task = asyncio.create_task(_run_claim(item))
                    self._inflight.add(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(
                    f"NeedsAgent dispatcher pass failed: {type(exc).__name__}"
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
