"""Durable, monotonic resource bind/close commands and pinned remote receipts.

Close blocks local admission in the acceptance transaction. Remote IO happens
after commit and can be replayed by any worker with the same command ID. These
commands neither grant human control nor certify physical-channel drainage.
"""
from contextlib import asynccontextmanager
import asyncio
from dataclasses import asdict
from datetime import timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from assistant import resource_control as controls
from assistant.commands import _authority, command_digest
from assistant.policy import AssistantError, lock_actor
from core.identifier import generate_id
from db.base import get_db_session
from db.models.assistant import AssistantCommand
from db.models.cloud_desktop import CloudDesktop
from session.internal_parts import begin_session_write


class RemoteControl(BaseModel):
    resource_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    epoch: int = Field(strict=True, ge=1)
    owner_kind: Literal["automation", "human"]
    owner_id: str = Field(min_length=1, max_length=64)
    admission: Literal["open", "closed"]


class RemoteStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    protocol: Literal["resource_admission_v2"]
    journal_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    control: RemoteControl | None
    blocking_count: int = Field(strict=True, ge=0)
    tracked_operations_drained: bool = Field(strict=True)
    remote_exclusivity_verified: Literal[False]


def conflict(code):
    return AssistantError(409, code, "Resource control requires inspection before continuing")


async def resource_locked(db, *, user_id, workspace_id, resource_id):
    await controls.actor(db, user_id, workspace_id)
    row = await controls.locked(db, resource_id)
    if row is None or row.workspace_id != workspace_id:
        raise AssistantError(404, "RESOURCE_UNAVAILABLE", "Resource is unavailable")
    desktop = await db.get(CloudDesktop, row.desktop_record_id)
    if (desktop is None or desktop.is_deleted or desktop.workspace_id != workspace_id
            or desktop.pool_state != "assigned" or row.provider != "wuying"
            or row.physical_id != f"{desktop.region_id}:{desktop.desktop_id}"):
        raise controls.unavailable()
    return row, desktop


async def read_resource(*, user_id, workspace_id, main_id, resource_id):
    async with get_db_session() as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        row, _ = await resource_locked(db, user_id=user_id, workspace_id=workspace_id, resource_id=resource_id)
        return {**await controls.drain_status_locked(db, row), "owner_kind": row.owner_kind,
            "owner_id": row.owner_id, "status": row.status, "remote_status": row.remote_status,
            "remote_journal_id": row.remote_journal_id, "human_takeover_available": False}


async def accept_resource_command(*, user_id, workspace_id, main_id, resource_id,
                                  action, expected_epoch, idempotency_key):
    if action not in {"bind", "close"} or type(expected_epoch) is not int or expected_epoch < 1:
        raise ValueError("A supported resource action and expected epoch are required")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 64:
        raise ValueError("A stable command key is required")
    digest = command_digest({"action": "resource_" + action, "resource_id": resource_id,
        "expected_epoch": expected_epoch})
    async with get_db_session() as db:
        await begin_session_write(db)
        await lock_actor(db, user_id)
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        row, _ = await resource_locked(db, user_id=user_id, workspace_id=workspace_id, resource_id=resource_id)
        prior = await db.scalar(select(AssistantCommand).where(AssistantCommand.actor_user_id == user_id,
            AssistantCommand.workspace_id == workspace_id, AssistantCommand.assistant_session_id == main_id,
            AssistantCommand.idempotency_key == idempotency_key))
        if prior:
            if prior.payload_digest != digest:
                raise conflict("ASSISTANT_COMMAND_CONFLICT")
            return dict(prior.receipt)
        fence = controls.fence_for(row)
        if fence.epoch != expected_epoch or fence.owner_kind != "automation" or fence.owner_id != workspace_id:
            raise conflict("RESOURCE_FENCE_CHANGED")
        if action == "bind" and (row.status != "active" or row.admission_state != "open" or row.epoch != 1):
            raise controls.unavailable()
        if action == "close":
            await controls.close_admission_locked(db, fence, user_id=user_id)
        stamp, command_id = await controls.clock(db), generate_id()
        receipt = {"command_id": command_id, "resource_id": resource_id, "epoch": fence.epoch,
            "action": action, "state": "accepted", "remote_exclusivity_verified": False}
        db.add(AssistantCommand(id=command_id, actor_user_id=user_id, workspace_id=workspace_id,
            assistant_session_id=main_id, idempotency_key=idempotency_key, action="resource_" + action,
            target_type="resource", target_id=resource_id, payload_digest=digest, expected_revision=expected_epoch,
            source_ref={"entrypoint": "assistant_resource_control", "actor_user_id": user_id,
                        "fence": asdict(fence)}, state="accepted", receipt=receipt,
            created_at=stamp, updated_at=stamp))
        return receipt


@asynccontextmanager
async def remote_client(desktop):
    # Resolve only the already assigned SQL record. No pool acquisition,
    # provisioning, directory initialization, SSH repair or cloud restart.
    from sandbox.channel import route_for_record
    from sandbox.client import SandboxClient
    host, port, key = route_for_record({column.name: getattr(desktop, column.name)
        for column in CloudDesktop.__table__.columns})
    client = SandboxClient(host, port, key, desktop_id=desktop.desktop_id, workspace_id=desktop.workspace_id)
    try:
        yield client
    finally:
        await client.aclose()


async def pending_locked(db, command_id):
    initial = await db.get(AssistantCommand, command_id)
    if initial is None or initial.target_type != "resource" or initial.action not in {"resource_bind", "resource_close"}:
        return None
    if initial.state not in {"accepted", "applying"}:
        return None
    await _authority(db, user_id=initial.actor_user_id, workspace_id=initial.workspace_id,
                     main_id=initial.assistant_session_id)
    row, desktop = await resource_locked(db, user_id=initial.actor_user_id,
        workspace_id=initial.workspace_id, resource_id=initial.target_id)
    command = await db.scalar(select(AssistantCommand).where(AssistantCommand.id == command_id)
        .with_for_update().execution_options(populate_existing=True))
    if command.state not in {"accepted", "applying"}:
        return None
    if asdict(controls.fence_for(row)) != command.source_ref.get("fence"):
        raise conflict("RESOURCE_FENCE_CHANGED")
    return command, row, desktop


def verified_status(payload, fence, journal_id=None):
    try:
        status = RemoteStatus.model_validate(payload)
    except ValidationError as exc:
        raise conflict("RESOURCE_PROTOCOL_UNAVAILABLE") from exc
    if journal_id is not None and status.journal_id != journal_id:
        raise conflict("RESOURCE_JOURNAL_CHANGED")
    if status.tracked_operations_drained != (status.blocking_count == 0):
        raise conflict("RESOURCE_RECEIPT_INVALID")
    if status.control is not None and {key: getattr(status.control, key) for key in fence} != fence:
        raise conflict("RESOURCE_FENCE_CHANGED")
    return status


async def dispatch(command_id, *, client_factory=None):
    """Replay monotonic remote commands, including after lost responses/restart."""
    try:
        async with get_db_session() as db:
            await begin_session_write(db)
            pending = await pending_locked(db, command_id)
            if pending is None:
                return False
            command, row, desktop = pending
            command.state, command.updated_at = "applying", await controls.clock(db)
            fence, action, pinned = dict(command.source_ref["fence"]), command.action.removeprefix("resource_"), row.remote_journal_id
        async with (client_factory or remote_client)(desktop) as client:
            status = verified_status(await client.resource_status(), fence, pinned)
            async with get_db_session() as db:
                await begin_session_write(db)
                pending = await pending_locked(db, command_id)
                if pending is None:
                    return False
                command, row, _ = pending
                if row.remote_journal_id is not None and row.remote_journal_id != status.journal_id:
                    raise conflict("RESOURCE_JOURNAL_CHANGED")
                # Commit this pin BEFORE the network write. A crash cannot
                # adopt a different empty journal on its next attempt.
                row.remote_journal_id = status.journal_id
                command.receipt = {**command.receipt, "remote_journal_id": status.journal_id}
            payload = {**fence, "command_id": command_id, "journal_id": status.journal_id}
            result = await client.resource_command(action, payload)
        observed = verified_status(result, fence, status.journal_id)
        receipt = result.get("command_receipt")
        if (not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in {**payload, "action": action}.items())
                or receipt.get("admission") not in {"open", "closed"} or observed.control is None
                or action == "close" and (receipt["admission"] != "closed" or observed.control.admission != "closed")):
            raise conflict("RESOURCE_RECEIPT_INVALID")
        async with get_db_session() as db:
            await begin_session_write(db)
            pending = await pending_locked(db, command_id)
            if pending is None:
                return False
            command, row, _ = pending
            if row.remote_journal_id != observed.journal_id:
                raise conflict("RESOURCE_JOURNAL_CHANGED")
            stamp = await controls.clock(db)
            previous = row.remote_status or {}
            # A delayed bind reply cannot overwrite a newer closed snapshot.
            if observed.control.admission == "closed" or (previous.get("control") or {}).get("admission") != "closed":
                row.remote_status = {**observed.model_dump(), "observed_at": stamp.isoformat()}
            if observed.control.admission == "closed":
                row.admission_state = "closed"
                if row.status != "hold":
                    row.status = "draining"
            row.updated_at = stamp
            command.state, command.updated_at = "applied", stamp
            command.receipt = {**command.receipt, "remote_receipt": {**payload, "action": action,
                "admission": receipt["admission"]}, "remote_exclusivity_verified": False}
            command.receipt.pop("error_code", None)
        return True
    except Exception as exc:
        # Error bodies can contain endpoint credentials. Persist only a fixed
        # code; transport failure remains retryable with the original ID/pin.
        blocked = isinstance(exc, AssistantError) or isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {400, 403, 409, 422, 423, 501}
        code = exc.code if isinstance(exc, AssistantError) else "RESOURCE_REMOTE_REJECTED" if blocked else "RESOURCE_REMOTE_UNAVAILABLE"
        async with get_db_session() as db:
            await begin_session_write(db)
            initial = await db.get(AssistantCommand, command_id)
            row = await controls.locked(db, initial.target_id) if initial is not None else None
            command = await db.scalar(select(AssistantCommand).where(AssistantCommand.id == command_id)
                .with_for_update().execution_options(populate_existing=True))
            if command is None or command.state not in {"accepted", "applying"}:
                return False
            command.state, command.updated_at = "blocked" if blocked else "applying", await controls.clock(db)
            command.receipt = {**command.receipt, "error_code": code}
            if (blocked and row is not None and (row.remote_journal_id is not None or command.action == "resource_close")
                    and row.workspace_id == command.workspace_id
                    and asdict(controls.fence_for(row)) == command.source_ref.get("fence")):
                row.status, row.admission_state, row.updated_at = "hold", "closed", await controls.clock(db)
        return False


async def recover_resource_commands(*, limit=4):
    async with get_db_session() as db:
        cutoff = await controls.clock(db) - timedelta(seconds=15)
        ids = list((await db.scalars(select(AssistantCommand.id).where(
            AssistantCommand.target_type == "resource", AssistantCommand.action.in_(("resource_bind", "resource_close")),
            AssistantCommand.state.in_(("accepted", "applying")), AssistantCommand.updated_at <= cutoff)
            .order_by(AssistantCommand.updated_at, AssistantCommand.id).limit(min(limit, 4)))).all())
    return sum(await asyncio.gather(*(dispatch(command_id) for command_id in ids)))
