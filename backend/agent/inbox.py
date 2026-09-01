"""Durable main-Agent input queue and exact driver wake protocol.

The public transcript is materialized only after a driver generation has been
reserved.  Acceptance, however, commits first, so an API worker can disappear
at every boundary without losing input or creating an unfenced Message.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from typing import Any, Callable, Literal, Sequence
import uuid
import weakref

from sqlalchemy import func, or_, select

from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_inbox import AgentInboxItem
from db.models.file_asset import FileAsset
from db.models.message import Message as MessageRow


log = create_logger("agent.inbox")

Delivery = Literal["followup", "steer", "inject"]
Target = Literal["next-turn", "next-step"]
InboxState = Literal["accepted", "claimed", "canceled", "settled"]

MAX_PROMPT_CHARS = 65_536
MAX_ATTACHMENTS = 32
MAX_OUTPUT_FORMAT_BYTES = 256 * 1024
MAX_STEP_BATCH = 32
CLAIM_SECONDS = 5 * 60
WAIT_POLL_SECONDS = 0.25
MAX_DURABLE_DELIVERY_ATTEMPTS = 3

DELIVERY_TERMINAL_ERROR = {
    "name": "AttachmentDeliveryError",
    "code": "ATTACHMENT_DELIVERY_FAILED",
    "message": (
        "A required attachment could not be delivered. "
        "This request was not sent to the model."
    ),
}


class InboxError(RuntimeError):
    """Base error for durable inbox invariants."""


class InboxIdempotencyConflict(InboxError):
    """A stable client id was reused for different input."""


class InboxAttachmentError(InboxError):
    """One or more accepted attachment identities cannot be materialized."""

    def __init__(self, item_ids: Sequence[str], message: str):
        super().__init__(message)
        self.item_ids = tuple(item_ids)


class InboxAttachmentDeliveryPending(InboxError):
    """A claimed attachment failed transiently and remains recoverable."""

    def __init__(self, item_ids: Sequence[str]):
        super().__init__("claimed attachment delivery remains pending")
        self.item_ids = tuple(item_ids)


@dataclass(frozen=True, slots=True)
class InboxReceipt:
    id: str
    user_id: str
    project_id: str
    session_id: str
    client_id: str | None
    delivery: Delivery
    target: Target
    state: InboxState
    message_id: str | None
    result_message_id: str | None
    run_id: str | None
    generation: int | None
    turn_id: str | None
    step_id: str | None
    outcome: str | None
    error: dict | None
    delivery_attempts: int
    delivery_last_error: dict | None
    created: bool = False


@dataclass(frozen=True, slots=True)
class ClaimedBatch:
    receipts: tuple[InboxReceipt, ...]
    attachment_ids: tuple[str, ...]
    messages: tuple[Any, ...]

    @property
    def empty(self) -> bool:
        return not self.receipts

    @property
    def claimed_next_turn(self) -> bool:
        return any(receipt.target == "next-turn" for receipt in self.receipts)


@dataclass(frozen=True, slots=True)
class AttachmentDeliveryResult:
    runnable_item_ids: tuple[str, ...]
    terminal_item_ids: tuple[str, ...]
    result_message_id: str | None = None
    direct_trigger: bool = False

    @property
    def should_run_provider(self) -> bool:
        return self.direct_trigger or bool(self.runnable_item_ids)


@dataclass(slots=True)
class _ItemWaiters:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    count: int = 0


_item_events: dict[str, _ItemWaiters] = {}
_wake_tasks: dict[tuple[str, str], asyncio.Task] = {}
_wake_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _wake_lock(key: tuple[str, str]) -> asyncio.Lock:
    lock = _wake_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _wake_locks[key] = lock
    return lock


def _notify(item_ids: Sequence[str]) -> None:
    for item_id in item_ids:
        waiters = _item_events.get(item_id)
        if waiters is not None:
            waiters.event.set()


def _acquire_item_waiter(item_id: str) -> _ItemWaiters:
    waiters = _item_events.get(item_id)
    if waiters is None:
        waiters = _ItemWaiters()
        _item_events[item_id] = waiters
    waiters.count += 1
    return waiters


def _release_item_waiter(item_id: str, waiters: _ItemWaiters) -> None:
    waiters.count -= 1
    if waiters.count <= 0 and _item_events.get(item_id) is waiters:
        _item_events.pop(item_id, None)


async def _database_utcnow(db) -> datetime:
    expression = (
        func.clock_timestamp()
        if db.get_bind().dialect.name == "postgresql"
        else func.current_timestamp()
    )
    result = await db.execute(select(expression))
    value = result.scalar_one()
    result.close()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _target(delivery: Delivery) -> Target:
    if delivery == "followup":
        return "next-turn"
    if delivery in {"steer", "inject"}:
        return "next-step"
    raise ValueError("unsupported inbox delivery")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _request_digest(
    *,
    delivery: Delivery,
    prompt: str,
    attachments: Sequence[str],
    agent: str | None,
    model: str | None,
    video_model: str | None,
    variant: str | None,
    output_format: dict | None,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "delivery": delivery,
                "prompt": prompt,
                "attachments": list(attachments),
                "agent": agent,
                "model": model,
                "video_model": video_model,
                "variant": variant,
                "output_format": output_format,
            }
        )
    ).hexdigest()


def _validate_input(
    *,
    prompt: str,
    attachments: Sequence[str],
    client_id: str | None,
    output_format: dict | None,
) -> tuple[str, ...]:
    if not isinstance(prompt, str) or not prompt or len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt must be 1..{MAX_PROMPT_CHARS} characters")
    if client_id is not None:
        if (
            not client_id
            or len(client_id) > 64
            or client_id.startswith(("sjr:", "tabort:"))
        ):
            raise ValueError("invalid or reserved inbox client id")
    if len(attachments) > MAX_ATTACHMENTS:
        raise ValueError(f"at most {MAX_ATTACHMENTS} attachments are allowed")
    normalized: list[str] = []
    for asset_id in attachments:
        if not isinstance(asset_id, str) or not asset_id or len(asset_id) > 64:
            raise ValueError("attachment ids must be 1..64 character strings")
        if asset_id in normalized:
            raise ValueError("attachment ids must be unique")
        normalized.append(asset_id)
    if output_format is not None:
        if not isinstance(output_format, dict):
            raise ValueError("output format must be an object")
        if len(_canonical_json(output_format)) > MAX_OUTPUT_FORMAT_BYTES:
            raise ValueError("output format is too large")
    return tuple(normalized)


def _receipt(row: AgentInboxItem, *, created: bool = False) -> InboxReceipt:
    return InboxReceipt(
        id=row.id,
        user_id=row.user_id,
        project_id=row.project_id,
        session_id=row.session_id,
        client_id=row.client_id,
        delivery=row.delivery,  # type: ignore[arg-type]
        target=row.target,  # type: ignore[arg-type]
        state=row.state,  # type: ignore[arg-type]
        message_id=row.message_id,
        result_message_id=row.result_message_id,
        run_id=row.run_id,
        generation=row.generation,
        turn_id=row.turn_id,
        step_id=row.step_id,
        outcome=row.outcome,
        error=dict(row.error) if isinstance(row.error, dict) else None,
        delivery_attempts=int(row.delivery_attempts or 0),
        delivery_last_error=(
            dict(row.delivery_last_error)
            if isinstance(row.delivery_last_error, dict)
            else None
        ),
        created=created,
    )


async def _owned_attachments_locked(
    db,
    *,
    user_id: str,
    attachment_ids: Sequence[str],
) -> dict[str, FileAsset]:
    if not attachment_ids:
        return {}
    rows = list(
        (
            await db.execute(
                select(FileAsset)
                .where(
                    FileAsset.id.in_(attachment_ids),
                    FileAsset.user_id == user_id,
                    FileAsset.status == "ready",
                    FileAsset.is_deleted.is_(False),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    return {row.id: row for row in rows}


async def _validate_owned_attachments_locked(
    db,
    *,
    user_id: str,
    attachment_ids: Sequence[str],
) -> dict[str, FileAsset]:
    by_id = await _owned_attachments_locked(
        db,
        user_id=user_id,
        attachment_ids=attachment_ids,
    )
    missing = [asset_id for asset_id in attachment_ids if asset_id not in by_id]
    if missing:
        raise InboxAttachmentError((), "attachment is missing, deleted, or not ready")
    return by_id


async def accept_inbox_item(
    *,
    session_id: str,
    user_id: str,
    delivery: Delivery,
    prompt: str,
    attachments: Sequence[str] = (),
    client_id: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    video_model: str | None = None,
    variant: str | None = None,
    output_format: dict | None = None,
) -> InboxReceipt:
    """Persist one idempotent input before attempting to own its Session."""
    target = _target(delivery)
    normalized_attachments = _validate_input(
        prompt=prompt,
        attachments=attachments,
        client_id=client_id,
        output_format=output_format,
    )
    digest = _request_digest(
        delivery=delivery,
        prompt=prompt,
        attachments=normalized_attachments,
        agent=agent,
        model=model,
        video_model=video_model,
        variant=variant,
        output_format=output_format,
    )
    created = False
    async with get_db_session() as db:
        from session.agent_event_log import (
            append_agent_event_locked,
            ensure_surface_seed_locked,
            prepare_agent_event_write,
        )

        owner = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=None,
        )
        existing = None
        if client_id is not None:
            existing = (
                await db.execute(
                    select(AgentInboxItem)
                    .where(
                        AgentInboxItem.user_id == user_id,
                        AgentInboxItem.session_id == session_id,
                        AgentInboxItem.client_id == client_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
        if existing is not None:
            if existing.request_digest != digest:
                raise InboxIdempotencyConflict(
                    "inbox client id is already bound to different input"
                )
            result = _receipt(existing)
        else:
            await _validate_owned_attachments_locked(
                db,
                user_id=user_id,
                attachment_ids=normalized_attachments,
            )
            now = await _database_utcnow(db)
            row = AgentInboxItem(
                id=ascending("inbox"),
                user_id=user_id,
                project_id=owner.project_id,
                session_id=session_id,
                client_id=client_id,
                request_digest=digest,
                delivery=delivery,
                target=target,
                prompt=prompt,
                attachments=list(normalized_attachments),
                agent=agent,
                model=model,
                video_model=video_model,
                variant=variant,
                output_format=output_format,
                state="accepted",
                message_id=None,
                result_message_id=None,
                run_id=None,
                generation=None,
                turn_id=None,
                step_id=None,
                claim_token=None,
                claim_owner=None,
                claim_expires_at=None,
                outcome=None,
                error=None,
                delivery_attempts=0,
                delivery_last_error=None,
                accepted_at=now,
                claimed_at=None,
                canceled_at=None,
                settled_at=None,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            await db.flush()
            await ensure_surface_seed_locked(db, owner)
            await append_agent_event_locked(
                db,
                owner,
                kind="inbox.accepted",
                payload={
                    "item_id": row.id,
                    "state": "accepted",
                    "delivery": delivery,
                    "target": target,
                    "client_id": client_id,
                    "request_digest": digest,
                    "attachment_count": len(normalized_attachments),
                },
                idempotency_key=f"inbox:{row.id}:accepted",
            )
            created = True
            result = _receipt(row, created=True)
    if created:
        _notify((result.id,))
    return result


async def get_inbox_item(
    item_id: str,
    *,
    user_id: str,
    session_id: str | None = None,
) -> InboxReceipt | None:
    async with get_db_session() as db:
        clauses = [AgentInboxItem.id == item_id, AgentInboxItem.user_id == user_id]
        if session_id is not None:
            clauses.append(AgentInboxItem.session_id == session_id)
        row = (
            await db.execute(select(AgentInboxItem).where(*clauses))
        ).scalar_one_or_none()
        return _receipt(row) if row is not None else None


async def wait_for_inbox_terminal(
    item_id: str,
    *,
    user_id: str,
    timeout: float | None = None,
) -> InboxReceipt:
    """Wait for this exact item, polling so another worker can settle it."""
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    waiters = _acquire_item_waiter(item_id)
    try:
        while True:
            receipt = await get_inbox_item(item_id, user_id=user_id)
            if receipt is None:
                raise LookupError("inbox item not found")
            if receipt.state in {"canceled", "settled"}:
                return receipt
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("inbox item did not settle before the deadline")
            waiters.event.clear()
            wait_for = (
                WAIT_POLL_SECONDS
                if remaining is None
                else min(WAIT_POLL_SECONDS, remaining)
            )
            try:
                await asyncio.wait_for(waiters.event.wait(), timeout=wait_for)
            except asyncio.TimeoutError:
                pass
    finally:
        _release_item_waiter(item_id, waiters)


async def _selected_boundary_rows(
    db,
    *,
    session_id: str,
    user_id: str,
    include_next_turn: bool,
) -> list[AgentInboxItem]:
    next_step = list(
        (
            await db.execute(
                select(AgentInboxItem)
                .where(
                    AgentInboxItem.session_id == session_id,
                    AgentInboxItem.user_id == user_id,
                    AgentInboxItem.state == "accepted",
                    AgentInboxItem.target == "next-step",
                )
                .order_by(AgentInboxItem.created_at, AgentInboxItem.id)
                .limit(MAX_STEP_BATCH)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not include_next_turn:
        return next_step
    next_turn = (
        await db.execute(
            select(AgentInboxItem)
            .where(
                AgentInboxItem.session_id == session_id,
                AgentInboxItem.user_id == user_id,
                AgentInboxItem.state == "accepted",
                AgentInboxItem.target == "next-turn",
            )
            .order_by(AgentInboxItem.created_at, AgentInboxItem.id)
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    return [*next_step, *(() if next_turn is None else (next_turn,))]


async def _claim_inbox_boundary_once(
    lease,
    *,
    step: int,
    include_next_turn: bool,
    deliver_attachments: bool = False,
    fault: Callable[[str], None] | None = None,
) -> ClaimedBatch:
    """Claim and materialize one exact boundary under the current run fence."""
    if step < 1:
        raise ValueError("inbox step must be positive")
    run_fence = (lease.session_id, lease.run_id, lease.generation)
    messages: list[Any] = []
    claimed_ids: list[str] = []
    attachment_ids: list[str] = []
    receipts: list[InboxReceipt] = []
    async with get_db_session() as db:
        from agent.driver import WORKER_ID, bind_trigger_message_locked
        from models.message import FilePart, FileRelation
        from project.workspace import asset_sandbox_path
        from session.agent_event_log import (
            append_agent_event_locked,
            prepare_agent_event_write,
        )
        from session.session import _insert_user_message_locked

        owner = await prepare_agent_event_write(
            db,
            session_id=lease.session_id,
            user_id=lease.user_id,
            run_fence=run_fence,
        )
        rows = await _selected_boundary_rows(
            db,
            session_id=lease.session_id,
            user_id=lease.user_id,
            include_next_turn=include_next_turn,
        )
        if not rows:
            return ClaimedBatch((), (), ())
        if fault is not None:
            fault("selected")

        all_assets: list[str] = []
        for row in rows:
            all_assets.extend(row.attachments or [])
        assets = await _owned_attachments_locked(
            db,
            user_id=lease.user_id,
            attachment_ids=tuple(dict.fromkeys(all_assets)),
        )
        invalid_rows = [
            row
            for row in rows
            if any(asset_id not in assets for asset_id in (row.attachments or []))
        ]
        if invalid_rows:
            # The transaction still rolls back as one unit, but only broken
            # queue rows are identified for cancellation. Valid neighbours stay
            # accepted and are claimed on the caller's immediate retry.
            raise InboxAttachmentError(
                [row.id for row in invalid_rows],
                "attachment is missing, deleted, or not ready",
            )

        message_ids = {row.id: ascending("message") for row in rows}
        existing_turn_id = (
            await db.execute(
                select(AgentInboxItem.turn_id)
                .where(
                    AgentInboxItem.session_id == lease.session_id,
                    AgentInboxItem.user_id == lease.user_id,
                    AgentInboxItem.run_id == lease.run_id,
                    AgentInboxItem.generation == lease.generation,
                    AgentInboxItem.state == "claimed",
                    AgentInboxItem.turn_id.is_not(None),
                )
                .order_by(
                    AgentInboxItem.claimed_at,
                    AgentInboxItem.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        driver_trigger = (
            await db.execute(
                select(AgentDriverState.trigger_message_id).where(
                    AgentDriverState.session_id == lease.session_id,
                    AgentDriverState.user_id == lease.user_id,
                    AgentDriverState.run_id == lease.run_id,
                    AgentDriverState.generation == lease.generation,
                )
            )
        ).scalar_one_or_none()
        # Turn identity is stable across every next-step boundary. A direct
        # regenerate/command uses its existing trigger; an Inbox-started turn
        # uses the first materialized boundary tail.
        turn_id = existing_turn_id or driver_trigger or message_ids[rows[-1].id]
        step_id = f"{lease.run_id}:{lease.generation}:{step}"
        now = await _database_utcnow(db)
        # SQLite's statement clock is second-resolution. A next-step claim in
        # that same second must still sort after the Assistant that requested
        # it; otherwise both SQL and event public Surfaces become U,U,U,U,A,A
        # instead of the canonical U,U,U,A,U,A sequence.
        latest_created = (
            await db.execute(
                select(func.max(MessageRow.created_at)).where(
                    MessageRow.session_id == lease.session_id,
                    MessageRow.user_id == lease.user_id,
                )
            )
        ).scalar_one_or_none()
        if latest_created is not None:
            if latest_created.tzinfo is None:
                latest_created = latest_created.replace(tzinfo=timezone.utc)
            if latest_created >= now:
                now = latest_created + timedelta(microseconds=1)
        for ordinal, row in enumerate(rows):
            message_id = message_ids[row.id]
            parts = []
            for asset_id in row.attachments or []:
                asset = assets[asset_id]
                if not asset.session_id:
                    asset.session_id = lease.session_id
                if not asset.project_id:
                    asset.project_id = owner.project_id
                parts.append(
                    FilePart(
                        id=ascending("part"),
                        path=asset_sandbox_path(
                            lease.user_id,
                            owner.project_id,
                            asset.name,
                            asset_id=asset.id,
                        ),
                        mime_type=asset.mime,
                        asset_id=asset.id,
                        oss_key=asset.oss_key,
                        size=asset.size,
                        relation=FileRelation(
                            group_id=f"message:{message_id}:attachments",
                            role="input",
                            kind="user_attachment",
                            label=asset.name,
                        ),
                        session_id=lease.session_id,
                        message_id=message_id,
                    )
                )
                if asset_id not in attachment_ids:
                    attachment_ids.append(asset_id)

            message = await _insert_user_message_locked(
                db,
                session_id=lease.session_id,
                text=row.prompt,
                agent=row.agent or owner.agent or "build",
                model=row.model or owner.model,
                synthetic=False,
                variant=row.variant,
                client_message_id=row.client_id,
                output_format=row.output_format,
                user_id=lease.user_id,
                run_fence=run_fence,
                logical_turn_id=turn_id,
                bind_trigger=False,
                message_id=message_id,
                additional_parts=tuple(parts),
                session_row=owner,
                now=now + timedelta(microseconds=ordinal),
            )
            claim_token = uuid.uuid4().hex
            row.state = "claimed"
            row.message_id = message_id
            row.run_id = lease.run_id
            row.generation = lease.generation
            row.turn_id = turn_id
            row.step_id = step_id
            row.claim_token = claim_token
            row.claim_owner = WORKER_ID
            row.claim_expires_at = now + timedelta(seconds=CLAIM_SECONDS)
            row.claimed_at = now
            row.updated_at = now
            await append_agent_event_locked(
                db,
                owner,
                kind="inbox.claimed",
                payload={
                    "item_id": row.id,
                    "state": "claimed",
                    "delivery": row.delivery,
                    "target": row.target,
                    "message_id": message_id,
                    "claim_token": claim_token,
                },
                run_fence=run_fence,
                turn_id=turn_id,
                step_id=step_id,
                message_id=message_id,
                idempotency_key=(
                    f"inbox:{row.id}:claimed:{lease.run_id}:{lease.generation}:{step_id}"
                ),
            )
            messages.append(message)
            claimed_ids.append(row.id)
        if fault is not None:
            fault("materialized")

        # The final Message is the context tail and therefore the exact replay
        # anchor for this boundary. Binding shares the transaction with every
        # Message/Part and claimed lifecycle event above.
        await bind_trigger_message_locked(
            db,
            session_id=lease.session_id,
            user_id=lease.user_id,
            run_id=lease.run_id,
            generation=lease.generation,
            message_id=turn_id,
        )
        final_row = rows[-1]
        if final_row.model and final_row.model != owner.model:
            owner.model = final_row.model
        if final_row.variant != owner.variant:
            owner.variant = final_row.variant
        if final_row.video_model is not None:
            owner.video_model = final_row.video_model.strip() or None
        owner.updated_at = now
        await db.flush()
        if fault is not None:
            fault("bound")
        receipts = [_receipt(row) for row in rows]

    from session.session import _publish_user_message

    for message in messages:
        _publish_user_message(message, user_id=lease.user_id, run_fence=run_fence)
    _notify(claimed_ids)
    if deliver_attachments and receipts:
        await deliver_claimed_attachments(
            lease,
            item_ids=claimed_ids,
            expected_asset_ids=attachment_ids,
        )
    return ClaimedBatch(tuple(receipts), tuple(attachment_ids), tuple(messages))


async def claim_inbox_boundary(
    lease,
    *,
    step: int,
    include_next_turn: bool,
    deliver_attachments: bool = False,
    fault: Callable[[str], None] | None = None,
) -> ClaimedBatch:
    """Claim a boundary while isolating invalid durable attachments per item.

    An asset can be deleted after acceptance but before a step boundary.  The
    one transaction that selected it is rolled back, then only the affected
    queue rows are canceled and selection is retried under the same exact
    Driver lease.  This keeps valid FIFO neighbours flowing both at the first
    turn and at later steer/inject boundaries.
    """
    while True:
        try:
            return await _claim_inbox_boundary_once(
                lease,
                step=step,
                include_next_turn=include_next_turn,
                deliver_attachments=deliver_attachments,
                fault=fault,
            )
        except InboxAttachmentError as exc:
            if not exc.item_ids:
                raise
            await cancel_inbox_items(
                session_id=lease.session_id,
                user_id=lease.user_id,
                item_ids=exc.item_ids,
                reason=str(exc),
            )


async def run_has_claimed_turn(lease) -> bool:
    """Whether this logical driver already started its one logical turn.

    A wake can begin with only ``next-step`` input (for example an idle
    ``steer``).  That still closes the generation's turn boundary: a followup
    accepted after the claim is busy input for the *next* generation, not a
    late addition to this one.
    """
    async with get_db_session() as db:
        if (
            await db.execute(
                select(AgentInboxItem.id)
                .where(
                    AgentInboxItem.session_id == lease.session_id,
                    AgentInboxItem.user_id == lease.user_id,
                    AgentInboxItem.run_id == lease.run_id,
                    AgentInboxItem.generation == lease.generation,
                    AgentInboxItem.state == "claimed",
                )
                .limit(1)
            )
        ).scalar_one_or_none() is not None:
            return True
        trigger = (
            await db.execute(
                select(AgentDriverState.trigger_message_id).where(
                    AgentDriverState.session_id == lease.session_id,
                    AgentDriverState.user_id == lease.user_id,
                    AgentDriverState.run_id == lease.run_id,
                    AgentDriverState.generation == lease.generation,
                )
            )
        ).scalar_one_or_none()
        # An exact claimed row above identifies an Inbox-started turn. Any
        # remaining bound trigger is explicit (regenerate/plan/command) and
        # owns the turn even when it points at a historical Inbox Message.
        return trigger is not None


async def has_pending_next_step(session_id: str, *, user_id: str) -> bool:
    async with get_db_session() as db:
        value = (
            await db.execute(
                select(AgentInboxItem.id)
                .where(
                    AgentInboxItem.session_id == session_id,
                    AgentInboxItem.user_id == user_id,
                    AgentInboxItem.state == "accepted",
                    AgentInboxItem.target == "next-step",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return value is not None


async def settle_claimed_inbox_items(
    lease,
    *,
    result_message_id: str | None,
    outcome: str,
    error: dict | None = None,
) -> tuple[str, ...]:
    """Settle every item consumed by one exact driver generation."""
    run_fence = (lease.session_id, lease.run_id, lease.generation)
    settled: list[str] = []
    async with get_db_session() as db:
        from session.agent_event_log import (
            append_agent_event_locked,
            prepare_agent_event_write,
        )

        owner = await prepare_agent_event_write(
            db,
            session_id=lease.session_id,
            user_id=lease.user_id,
            run_fence=run_fence,
        )
        if result_message_id is not None:
            result_exists = (
                await db.execute(
                    select(MessageRow.id).where(
                        MessageRow.id == result_message_id,
                        MessageRow.session_id == lease.session_id,
                        MessageRow.user_id == lease.user_id,
                    )
                )
            ).scalar_one_or_none()
            if result_exists is None:
                raise InboxError("inbox result Message does not belong to its Session")
        rows = list(
            (
                await db.execute(
                    select(AgentInboxItem)
                    .where(
                        AgentInboxItem.session_id == lease.session_id,
                        AgentInboxItem.user_id == lease.user_id,
                        AgentInboxItem.run_id == lease.run_id,
                        AgentInboxItem.generation == lease.generation,
                        AgentInboxItem.state == "claimed",
                    )
                    .order_by(AgentInboxItem.created_at, AgentInboxItem.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        now = await _database_utcnow(db)
        for row in rows:
            if row.message_id is None:
                raise InboxError("terminal attachment failure has no user Message")
            row.state = "settled"
            row.result_message_id = result_message_id
            row.outcome = outcome[:24]
            row.error = error
            row.claim_expires_at = None
            row.settled_at = now
            row.updated_at = now
            await append_agent_event_locked(
                db,
                owner,
                kind="inbox.settled",
                payload={
                    "item_id": row.id,
                    "state": "settled",
                    "outcome": row.outcome,
                    "result_message_id": result_message_id,
                    "error": error,
                },
                run_fence=run_fence,
                turn_id=row.turn_id,
                step_id=row.step_id,
                message_id=row.message_id,
                idempotency_key=(
                    f"inbox:{row.id}:settled:{lease.run_id}:{lease.generation}"
                ),
            )
            settled.append(row.id)
    _notify(settled)
    return tuple(settled)


async def cancel_inbox_items(
    *,
    session_id: str,
    user_id: str,
    item_ids: Sequence[str] | None = None,
    reason: str = "canceled",
) -> tuple[str, ...]:
    """Cancel accepted input only; a claimed item belongs to its exact run."""
    canceled: list[str] = []
    async with get_db_session() as db:
        from session.agent_event_log import (
            append_agent_event_locked,
            ensure_surface_seed_locked,
            prepare_agent_event_write,
        )

        owner = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=None,
        )
        query = select(AgentInboxItem).where(
            AgentInboxItem.session_id == session_id,
            AgentInboxItem.user_id == user_id,
            AgentInboxItem.state == "accepted",
        )
        if item_ids is not None:
            if not item_ids:
                return ()
            query = query.where(AgentInboxItem.id.in_(item_ids))
        rows = list(
            (
                await db.execute(
                    query.order_by(
                        AgentInboxItem.created_at, AgentInboxItem.id
                    ).with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return ()
        await ensure_surface_seed_locked(db, owner)
        now = await _database_utcnow(db)
        for row in rows:
            row.state = "canceled"
            row.outcome = "canceled"
            row.error = {"message": reason[:512]}
            row.canceled_at = now
            row.updated_at = now
            await append_agent_event_locked(
                db,
                owner,
                kind="inbox.canceled",
                payload={
                    "item_id": row.id,
                    "state": "canceled",
                    "reason": reason[:512],
                },
                idempotency_key=f"inbox:{row.id}:canceled",
            )
            canceled.append(row.id)
    _notify(canceled)
    return tuple(canceled)


async def _has_waking_input(session_id: str, user_id: str) -> bool:
    async with get_db_session() as db:
        return (
            await db.execute(
                select(AgentInboxItem.id)
                .where(
                    AgentInboxItem.session_id == session_id,
                    AgentInboxItem.user_id == user_id,
                    AgentInboxItem.state == "accepted",
                    AgentInboxItem.delivery.in_(("followup", "steer")),
                )
                .limit(1)
            )
        ).scalar_one_or_none() is not None


async def _reserve_and_claim(session_id: str, user_id: str):
    if not await _has_waking_input(session_id, user_id):
        return None
    from agent.driver import DriverBusyError, DriverRecoveryRequiredError, reserve_run

    try:
        lease = await reserve_run(session_id, user_id)
    except (DriverBusyError, DriverRecoveryRequiredError, LookupError):
        return None
    try:
        while True:
            try:
                batch = await claim_inbox_boundary(
                    lease,
                    step=1,
                    include_next_turn=True,
                    deliver_attachments=False,
                )
            except InboxAttachmentError as exc:
                await cancel_inbox_items(
                    session_id=session_id,
                    user_id=user_id,
                    item_ids=exc.item_ids,
                    reason=str(exc),
                )
                # The failed claim committed nothing and did not bind a trigger.
                # Keep this reservation and immediately try the remaining FIFO
                # input instead of waiting for recovery or another API wake.
                continue
            if batch.empty:
                await lease.release(session_status="idle")
                return None
            return lease, batch
    except BaseException:
        await lease.release(session_status="error")
        raise


@dataclass(frozen=True, slots=True)
class _ClaimedDeliveryItem:
    id: str
    attachments: tuple[str, ...]
    delivery_attempts: int
    delivery_last_error: dict | None


async def _claimed_delivery_items(
    lease,
    *,
    item_ids: Sequence[str] | None,
) -> tuple[_ClaimedDeliveryItem, ...]:
    async with get_db_session() as db:
        query = select(AgentInboxItem).where(
            AgentInboxItem.session_id == lease.session_id,
            AgentInboxItem.user_id == lease.user_id,
            AgentInboxItem.run_id == lease.run_id,
            AgentInboxItem.generation == lease.generation,
            AgentInboxItem.state == "claimed",
        )
        if item_ids is not None:
            if not item_ids:
                return ()
            query = query.where(AgentInboxItem.id.in_(item_ids))
        rows = list(
            (
                await db.execute(
                    query.order_by(
                        AgentInboxItem.created_at,
                        AgentInboxItem.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(
            _ClaimedDeliveryItem(
                id=row.id,
                attachments=tuple(row.attachments or ()),
                delivery_attempts=int(row.delivery_attempts or 0),
                delivery_last_error=(
                    dict(row.delivery_last_error)
                    if isinstance(row.delivery_last_error, dict)
                    else None
                ),
            )
            for row in rows
        )


def _safe_delivery_failure(
    error: BaseException,
    *,
    expected_asset_ids: Sequence[str],
) -> dict:
    """Return bounded, non-secret evidence suitable for SQL and AgentEvent."""
    from sandbox.assets import AssetDeliveryError

    if isinstance(error, AssetDeliveryError):
        code = (
            error.code
            if error.code in {"asset_unavailable", "delivery_failed"}
            else "delivery_failed"
        )
        retryable = bool(error.retryable)
        expected = set(expected_asset_ids)
        missing = [
            asset_id[:64]
            for asset_id in error.missing_asset_ids
            if isinstance(asset_id, str) and asset_id in expected
        ][:MAX_ATTACHMENTS]
    else:
        code = "delivery_failed"
        retryable = True
        missing = []
    return {
        "code": code,
        "message": (
            "The attachment is no longer available."
            if not retryable
            else "Attachment transfer did not complete."
        ),
        "retryable": retryable,
        "missing_asset_ids": missing,
    }


async def _record_delivery_failure(
    lease,
    *,
    item_id: str,
    error: dict,
) -> tuple[int, bool]:
    """Increment one exact claim's durable attempt counter under its fence."""
    run_fence = (lease.session_id, lease.run_id, lease.generation)
    async with get_db_session() as db:
        from session.agent_event_log import (
            append_agent_event_locked,
            prepare_agent_event_write,
        )

        owner = await prepare_agent_event_write(
            db,
            session_id=lease.session_id,
            user_id=lease.user_id,
            run_fence=run_fence,
        )
        row = (
            await db.execute(
                select(AgentInboxItem)
                .where(
                    AgentInboxItem.id == item_id,
                    AgentInboxItem.session_id == lease.session_id,
                    AgentInboxItem.user_id == lease.user_id,
                    AgentInboxItem.run_id == lease.run_id,
                    AgentInboxItem.generation == lease.generation,
                    AgentInboxItem.state == "claimed",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise InboxError("attachment delivery claim lost its exact inbox fence")
        attempt = min(int(row.delivery_attempts or 0) + 1, 1000)
        durable_error = {**error, "attempt": attempt}
        row.delivery_attempts = attempt
        row.delivery_last_error = durable_error
        row.updated_at = await _database_utcnow(db)
        await append_agent_event_locked(
            db,
            owner,
            kind="inbox.claimed",
            payload={
                "item_id": row.id,
                "state": "claimed",
                "delivery_status": "failed",
                "attempt": attempt,
                "error": durable_error,
            },
            run_fence=run_fence,
            turn_id=row.turn_id,
            step_id=row.step_id,
            message_id=row.message_id,
            idempotency_key=(
                f"inbox:{row.id}:delivery_failed:"
                f"{lease.run_id}:{lease.generation}:{attempt}"
            ),
        )
        terminal = (
            not bool(durable_error.get("retryable"))
            or attempt >= MAX_DURABLE_DELIVERY_ATTEMPTS
        )
        return attempt, terminal


async def _record_delivery_success(lease, *, item_id: str) -> None:
    """Clear a resolved last error while retaining the cumulative attempt count."""
    run_fence = (lease.session_id, lease.run_id, lease.generation)
    async with get_db_session() as db:
        from session.agent_event_log import (
            append_agent_event_locked,
            prepare_agent_event_write,
        )

        owner = await prepare_agent_event_write(
            db,
            session_id=lease.session_id,
            user_id=lease.user_id,
            run_fence=run_fence,
        )
        row = (
            await db.execute(
                select(AgentInboxItem)
                .where(
                    AgentInboxItem.id == item_id,
                    AgentInboxItem.session_id == lease.session_id,
                    AgentInboxItem.user_id == lease.user_id,
                    AgentInboxItem.run_id == lease.run_id,
                    AgentInboxItem.generation == lease.generation,
                    AgentInboxItem.state == "claimed",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise InboxError("attachment delivery claim lost its exact inbox fence")
        if row.delivery_last_error is None:
            return
        row.delivery_last_error = None
        row.updated_at = await _database_utcnow(db)
        await append_agent_event_locked(
            db,
            owner,
            kind="inbox.claimed",
            payload={
                "item_id": row.id,
                "state": "claimed",
                "delivery_status": "succeeded",
                "attempts": int(row.delivery_attempts or 0),
            },
            run_fence=run_fence,
            turn_id=row.turn_id,
            step_id=row.step_id,
            message_id=row.message_id,
            idempotency_key=(
                f"inbox:{row.id}:delivery_succeeded:"
                f"{lease.run_id}:{lease.generation}:{row.delivery_attempts}"
            ),
        )


async def _settle_delivery_failures(
    lease,
    *,
    item_ids: Sequence[str],
    close_turn: bool,
) -> tuple[tuple[str, ...], str | None]:
    """Atomically close terminal delivery failures and, when needed, their turn."""
    if not item_ids:
        return (), None
    run_fence = (lease.session_id, lease.run_id, lease.generation)
    settled: list[str] = []
    assistant_id: str | None = None
    assistant_payload: dict | None = None
    assistant_update: dict | None = None
    async with get_db_session() as db:
        from session.agent_event_log import (
            append_agent_event_locked,
            append_message_events_locked,
            ensure_surface_seed_locked,
            prepare_agent_event_write,
        )

        owner = await prepare_agent_event_write(
            db,
            session_id=lease.session_id,
            user_id=lease.user_id,
            run_fence=run_fence,
        )
        rows = list(
            (
                await db.execute(
                    select(AgentInboxItem)
                    .where(
                        AgentInboxItem.id.in_(item_ids),
                        AgentInboxItem.session_id == lease.session_id,
                        AgentInboxItem.user_id == lease.user_id,
                        AgentInboxItem.run_id == lease.run_id,
                        AgentInboxItem.generation == lease.generation,
                        AgentInboxItem.state == "claimed",
                    )
                    .order_by(
                        AgentInboxItem.created_at,
                        AgentInboxItem.id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return (), None
        now = await _database_utcnow(db)
        if close_turn:
            parent_id = rows[-1].message_id
            if parent_id is None:
                raise InboxError("terminal attachment failure has no user Message")
            parent = (
                await db.execute(
                    select(MessageRow).where(
                        MessageRow.id == parent_id,
                        MessageRow.session_id == lease.session_id,
                        MessageRow.user_id == lease.user_id,
                    )
                )
            ).scalar_one_or_none()
            if parent is None:
                raise InboxError("terminal attachment failure lost its user Message")
            latest_created = (
                await db.execute(
                    select(func.max(MessageRow.created_at)).where(
                        MessageRow.session_id == lease.session_id,
                        MessageRow.user_id == lease.user_id,
                    )
                )
            ).scalar_one_or_none()
            if latest_created is not None:
                if latest_created.tzinfo is None:
                    latest_created = latest_created.replace(tzinfo=timezone.utc)
                if latest_created >= now:
                    now = latest_created + timedelta(microseconds=1)
            await ensure_surface_seed_locked(db, owner)
            assistant_id = ascending("message")
            assistant = MessageRow(
                id=assistant_id,
                session_id=lease.session_id,
                user_id=lease.user_id,
                role="assistant",
                parent_id=parent_id,
                model_id=parent.model or parent.model_id,
                agent=parent.agent,
                finish=None,
                error=None,
                created_at=now,
            )
            db.add(assistant)
            await db.flush()
            await append_message_events_locked(
                db,
                owner,
                assistant,
                operation="created",
                run_fence=run_fence,
                logical_turn_id=rows[-1].turn_id,
            )
            assistant.finish = "error"
            assistant.error = dict(DELIVERY_TERMINAL_ERROR)
            await db.flush()
            await append_message_events_locked(
                db,
                owner,
                assistant,
                operation="updated",
                run_fence=run_fence,
                logical_turn_id=rows[-1].turn_id,
            )
            assistant_payload = {
                "id": assistant.id,
                "session_id": lease.session_id,
                "role": "assistant",
                "parts": [],
                "created_at": now.isoformat(),
                "parent_id": parent_id,
                "model": assistant.model_id,
                "agent": assistant.agent,
            }
            assistant_update = {
                "id": assistant.id,
                "role": "assistant",
                "finish": "error",
                "error": dict(DELIVERY_TERMINAL_ERROR),
            }

        for row in rows:
            row.state = "settled"
            row.result_message_id = assistant_id
            row.outcome = "delivery_error"
            row.error = {
                **DELIVERY_TERMINAL_ERROR,
                "delivery_attempts": int(row.delivery_attempts or 0),
                "last_error": (
                    dict(row.delivery_last_error)
                    if isinstance(row.delivery_last_error, dict)
                    else None
                ),
            }
            row.claim_expires_at = None
            row.settled_at = now
            row.updated_at = now
            excluded_message_ids = [row.message_id]
            if assistant_id is not None:
                excluded_message_ids.append(assistant_id)
            await append_agent_event_locked(
                db,
                owner,
                kind="surface.model_exclusion",
                payload={
                    "message_ids": excluded_message_ids,
                    "reason": "inbox_delivery_error",
                    "source_item_id": row.id,
                },
                run_fence=run_fence,
                turn_id=row.turn_id,
                step_id=row.step_id,
                message_id=row.message_id,
                idempotency_key=(
                    f"inbox:{row.id}:model-exclusion:"
                    f"{lease.run_id}:{lease.generation}"
                ),
            )
            await append_agent_event_locked(
                db,
                owner,
                kind="inbox.settled",
                payload={
                    "item_id": row.id,
                    "state": "settled",
                    "outcome": row.outcome,
                    "result_message_id": assistant_id,
                    "error": row.error,
                },
                run_fence=run_fence,
                turn_id=row.turn_id,
                step_id=row.step_id,
                message_id=row.message_id,
                idempotency_key=(
                    f"inbox:{row.id}:settled:{lease.run_id}:{lease.generation}"
                ),
            )
            settled.append(row.id)

    _notify(settled)
    if assistant_payload is not None and assistant_update is not None:
        from bus import bus
        from bus.events import MESSAGE_CREATED, MESSAGE_UPDATED

        base = {
            "userId": lease.user_id,
            "sessionId": lease.session_id,
            "generation": lease.generation,
        }
        bus.publish(MESSAGE_CREATED, {**base, "message": assistant_payload})
        bus.publish(MESSAGE_UPDATED, {**base, "message": assistant_update})
    return tuple(settled), assistant_id


async def deliver_claimed_attachments(
    lease,
    *,
    item_ids: Sequence[str] | None = None,
    expected_asset_ids: Sequence[str] | None = None,
) -> AttachmentDeliveryResult:
    """Strictly deliver each exact claimed item with bounded durable recovery.

    A failed item never lets its attachment contract degrade to best effort.
    Permanent failures settle only that item; transient failures preserve the
    exact reserved generation until another recovery pass. If the entire
    boundary is terminal, a fenced Assistant error closes the public turn so
    neither this worker nor a future recovery invokes the model.
    """
    rows = await _claimed_delivery_items(lease, item_ids=item_ids)
    expected = tuple(dict.fromkeys(expected_asset_ids or ()))
    if not rows:
        if expected:
            from sandbox.assets import deliver_asset_ids

            await deliver_asset_ids(
                lease.session_id,
                lease.user_id,
                list(expected),
                strict=True,
                expected_asset_ids=list(expected),
            )
        return AttachmentDeliveryResult((), (), direct_trigger=True)

    row_expected = tuple(
        dict.fromkeys(asset_id for row in rows for asset_id in row.attachments)
    )
    if expected_asset_ids is not None and set(expected) != set(row_expected):
        raise InboxError("claimed attachment set drifted from its durable Parts")

    runnable: list[str] = []
    terminal: list[str] = []
    pending: list[str] = []
    from sandbox.assets import deliver_asset_ids

    for row in rows:
        if not row.attachments:
            runnable.append(row.id)
            continue
        prior = row.delivery_last_error or {}
        if (
            prior.get("retryable") is False
            or row.delivery_attempts >= MAX_DURABLE_DELIVERY_ATTEMPTS
        ):
            terminal.append(row.id)
            continue
        try:
            await deliver_asset_ids(
                lease.session_id,
                lease.user_id,
                list(row.attachments),
                strict=True,
                expected_asset_ids=list(row.attachments),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            safe_error = _safe_delivery_failure(
                exc,
                expected_asset_ids=row.attachments,
            )
            _attempt, is_terminal = await _record_delivery_failure(
                lease,
                item_id=row.id,
                error=safe_error,
            )
            (terminal if is_terminal else pending).append(row.id)
        else:
            if row.delivery_last_error is not None:
                await _record_delivery_success(lease, item_id=row.id)
            runnable.append(row.id)

    result_message_id = None
    if terminal:
        _settled, result_message_id = await _settle_delivery_failures(
            lease,
            item_ids=terminal,
            close_turn=not runnable and not pending,
        )
    if pending:
        raise InboxAttachmentDeliveryPending(pending)
    return AttachmentDeliveryResult(
        runnable_item_ids=tuple(runnable),
        terminal_item_ids=tuple(terminal),
        result_message_id=result_message_id,
    )


async def _deliver_claimed_batch(
    lease,
    batch: ClaimedBatch,
) -> AttachmentDeliveryResult:
    return await deliver_claimed_attachments(
        lease,
        item_ids=[receipt.id for receipt in batch.receipts],
        expected_asset_ids=batch.attachment_ids,
    )


async def _drive_claimed(lease, batch: ClaimedBatch) -> None:
    current = (lease, batch)
    while current is not None:
        active_lease, active_batch = current
        try:
            delivery = await _deliver_claimed_batch(active_lease, active_batch)
        except asyncio.CancelledError:
            await active_lease.preserve_for_recovery(session_status="error")
            raise
        except Exception:
            # Message/Parts and claim are already durable. Running the model
            # without those declared files would be a false success, so expire
            # this exact reserved marker for strict recovery instead.
            log.exception(
                "Inbox strict attachment delivery failed session=%s generation=%s",
                active_lease.session_id,
                active_lease.generation,
            )
            await active_lease.preserve_for_recovery(session_status="error")
            return
        if not delivery.should_run_provider:
            await active_lease.release(session_status="error")
            current = await _reserve_and_claim(
                active_lease.session_id,
                active_lease.user_id,
            )
            continue
        from agent.loop import run_loop

        try:
            await run_loop(
                active_lease.session_id,
                user_id=active_lease.user_id,
                lease=active_lease,
            )
        except asyncio.CancelledError:
            # ``run_loop`` normally settles its own lease. If cancellation
            # lands just before that boundary, this idempotently preserves the
            # claimed generation for recovery rather than leaving a renewing
            # orphan in the local activity registry.
            await active_lease.preserve_for_recovery(session_status="error")
            raise
        except Exception:
            await active_lease.preserve_for_recovery(session_status="error")
            raise
        current = await _reserve_and_claim(
            active_lease.session_id,
            active_lease.user_id,
        )


async def wake_inbox_session(session_id: str, user_id: str) -> str | None:
    """Reserve/claim now and start the loop only after that commit succeeds."""
    key = (user_id, session_id)
    async with _wake_lock(key):
        existing = _wake_tasks.get(key)
        if existing is not None and not existing.done():
            return None
        claimed = await _reserve_and_claim(session_id, user_id)
        if claimed is None:
            return None
        lease, batch = claimed
        task = asyncio.create_task(
            _drive_claimed(lease, batch),
            name=f"agent-inbox-wake:{session_id}:{lease.generation}",
        )
        _wake_tasks[key] = task

    def done(completed: asyncio.Task) -> None:
        if _wake_tasks.get(key) is completed:
            _wake_tasks.pop(key, None)
        try:
            completed.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Inbox driver failed session=%s", session_id)

    task.add_done_callback(done)
    return lease.run_id


def schedule_inbox_wake(session_id: str, user_id: str) -> None:
    """Best-effort local wake; periodic recovery is the durable fallback."""

    async def wake() -> None:
        try:
            await wake_inbox_session(session_id, user_id)
        except Exception:
            log.exception("Could not schedule inbox wake session=%s", session_id)

    asyncio.create_task(wake(), name=f"agent-inbox-dispatch:{session_id}")


async def claimable_inbox_sessions() -> list[tuple[str, str]]:
    """FIFO waking candidates for a recovery pass, independent of Drivers."""
    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(
                    AgentInboxItem.session_id,
                    AgentInboxItem.user_id,
                    func.min(AgentInboxItem.created_at).label("oldest"),
                )
                .where(
                    AgentInboxItem.state == "accepted",
                    AgentInboxItem.delivery.in_(("followup", "steer")),
                )
                .group_by(
                    AgentInboxItem.session_id,
                    AgentInboxItem.user_id,
                )
                .order_by("oldest", AgentInboxItem.session_id)
            )
        ).all()
        return [(row.session_id, row.user_id) for row in rows]


async def resume_claimable_inbox_sessions() -> list[str]:
    resumed: list[str] = []
    for session_id, user_id in await claimable_inbox_sessions():
        run_id = await wake_inbox_session(session_id, user_id)
        if run_id is not None:
            resumed.append(session_id)
    return resumed


async def rebind_recovered_claims(record, lease) -> int:
    """Move claimed inbox ownership to an exact reserved-driver takeover."""
    run_fence = (lease.session_id, lease.run_id, lease.generation)
    rebound: list[str] = []
    async with get_db_session() as db:
        from agent.driver import WORKER_ID
        from session.agent_event_log import (
            append_agent_event_locked,
            prepare_agent_event_write,
        )

        owner = await prepare_agent_event_write(
            db,
            session_id=lease.session_id,
            user_id=lease.user_id,
            run_fence=run_fence,
        )
        rows = list(
            (
                await db.execute(
                    select(AgentInboxItem)
                    .where(
                        AgentInboxItem.session_id == record.session_id,
                        AgentInboxItem.user_id == record.user_id,
                        AgentInboxItem.run_id == record.run_id,
                        AgentInboxItem.generation == record.generation,
                        AgentInboxItem.state == "claimed",
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        now = await _database_utcnow(db)
        for row in rows:
            row.run_id = lease.run_id
            row.generation = lease.generation
            row.claim_token = uuid.uuid4().hex
            row.claim_owner = WORKER_ID
            row.claim_expires_at = now + timedelta(seconds=CLAIM_SECONDS)
            row.updated_at = now
            await append_agent_event_locked(
                db,
                owner,
                kind="inbox.claimed",
                payload={
                    "item_id": row.id,
                    "state": "claimed",
                    "message_id": row.message_id,
                    "claim_token": row.claim_token,
                    "recovered_from": {
                        "run_id": record.run_id,
                        "generation": record.generation,
                    },
                },
                run_fence=run_fence,
                turn_id=row.turn_id,
                step_id=row.step_id,
                message_id=row.message_id,
                idempotency_key=(
                    f"inbox:{row.id}:rebound:{lease.run_id}:{lease.generation}"
                ),
            )
            rebound.append(row.id)
    _notify(rebound)
    return len(rebound)


async def settle_orphaned_claims() -> int:
    """Converge terminal claims whose owning Driver already disappeared.

    A claim with a live or expired non-idle Driver remains Driver recovery's
    responsibility.  This scanner covers the narrow crash after transcript
    completion/release but before inbox settlement committed.
    """
    async with get_db_session() as db:
        candidates = list(
            (
                await db.execute(
                    select(AgentInboxItem)
                    .where(
                        AgentInboxItem.state == "claimed",
                        AgentInboxItem.claim_expires_at.is_not(None),
                        AgentInboxItem.claim_expires_at <= func.current_timestamp(),
                    )
                    .order_by(AgentInboxItem.created_at, AgentInboxItem.id)
                )
            )
            .scalars()
            .all()
        )
    settled = 0
    seen: set[tuple[str, str, str, int]] = set()
    for item in candidates:
        if not item.run_id or item.generation is None:
            continue
        identity = (item.session_id, item.user_id, item.run_id, item.generation)
        if identity in seen:
            continue
        seen.add(identity)
        async with get_db_session() as db:
            driver = (
                await db.execute(
                    select(AgentDriverState).where(
                        AgentDriverState.session_id == item.session_id,
                    )
                )
            ).scalar_one_or_none()
            if driver is not None and driver.phase != "idle":
                continue
            trigger_ids = list(
                (
                    await db.execute(
                        select(
                            AgentInboxItem.message_id,
                        ).where(
                            AgentInboxItem.session_id == item.session_id,
                            AgentInboxItem.user_id == item.user_id,
                            AgentInboxItem.run_id == item.run_id,
                            AgentInboxItem.generation == item.generation,
                            AgentInboxItem.state == "claimed",
                            AgentInboxItem.message_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not trigger_ids:
                continue
            terminal = (
                await db.execute(
                    select(MessageRow)
                    .where(
                        MessageRow.session_id == item.session_id,
                        MessageRow.user_id == item.user_id,
                        MessageRow.role == "assistant",
                        MessageRow.parent_id.in_(trigger_ids),
                        or_(
                            MessageRow.finish.is_not(None),
                            MessageRow.error.is_not(None),
                        ),
                    )
                    .order_by(MessageRow.created_at.desc(), MessageRow.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if terminal is None:
            continue
        # There is no live fence left, so settle through a short exact
        # maintenance generation instead of writing lifecycle evidence unfenced.
        from agent.driver import (
            DriverBusyError,
            DriverRecoveryRequiredError,
            reserve_run,
        )

        try:
            lease = await reserve_run(
                item.session_id, item.user_id, initial_phase="finalizing"
            )
        except (DriverBusyError, DriverRecoveryRequiredError, LookupError):
            continue
        try:
            # Rebind the old logical claim to the maintenance generation before
            # the ordinary settle helper records its exact new fence.
            old = SimpleNamespace(
                session_id=item.session_id,
                user_id=item.user_id,
                run_id=item.run_id,
                generation=item.generation,
            )
            await rebind_recovered_claims(old, lease)
            settled += len(
                await settle_claimed_inbox_items(
                    lease,
                    result_message_id=terminal.id,
                    outcome="recovered",
                )
            )
            await lease.release(session_status="idle")
        except BaseException:
            await lease.preserve_for_recovery(session_status="error")
            raise
    return settled


async def quiesce_inbox_tasks(*, timeout: float = 10.0) -> None:
    tasks = [task for task in _wake_tasks.values() if not task.done()]
    if not tasks:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(
                *(asyncio.shield(task) for task in tasks), return_exceptions=True
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # Do not cancel durable Agent work during normal shutdown; its Driver
        # expiry and accepted/claimed inbox rows are the next process's recovery.
        log.warning("Inbox drivers did not quiesce within %.1fs", timeout)
