"""Loopback-facing infrastructure callbacks."""
from __future__ import annotations

import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from auth.mobile import validate_claims
from core.config import get_config
from core.log import create_logger
from db.base import get_db_session
from db.models.audit_log import AuditLog
from db.models.user import User
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from trajectory.config import admin_enabled

log = create_logger("api.internal")

router = APIRouter(prefix="/api/internal", tags=["internal"], include_in_schema=False)


@router.get("/tunnel-keys", response_class=PlainTextResponse)
async def tunnel_keys(
    fingerprint: str = Query(min_length=8, max_length=128),
    x_internal_token: str = Header(default=""),
):
    expected = get_config().internal_api_token
    if not expected or not hmac.compare_digest(x_internal_token, expected):
        return PlainTextResponse("", status_code=403)
    record = await cloud_desktop_repo.get_by_fingerprint(fingerprint)
    if (
        not record
        or record.get("tunnel_state") == "revoked"
        or not record.get("tunnel_pubkey")
        or not record.get("tunnel_port")
        or not record.get("tunnel_bind")
    ):
        return PlainTextResponse("")
    options = (
        'restrict,port-forwarding,'
        f'permitlisten="{record["tunnel_bind"]}:{record["tunnel_port"]}"'
    )
    return PlainTextResponse(f'{options} {record["tunnel_pubkey"]}\n')


def internal_token(x_internal_token: str = Header(default="")) -> None:
    """The tunnel-keys guard: X-Internal-Token must equal INTERNAL_API_TOKEN, which must be set.

    As a dependency it runs before body validation, so an unauthenticated
    caller learns nothing about the request schema.
    """
    expected = get_config().internal_api_token
    if not expected or not hmac.compare_digest(x_internal_token.encode(), expected.encode()):
        raise HTTPException(status_code=403)


# -- Trajectory worker callbacks (SPEC §8.12) --

TRAJECTORY_AUDIT_ACTIONS = frozenset({
    "admin.trajectory.list", "admin.trajectory.view", "admin.trajectory.payload",
    "admin.trajectory.export", "admin.trajectory.download", "trajectory.subscribe",
})


class TrajectoryViewerQuery(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    client: str | None = Field(default=None, max_length=32)
    sid: str | None = Field(default=None, max_length=128)
    # Correlation only: the worker checks the token blacklist it shares with the backend.
    jti: str | None = Field(default=None, max_length=128)


async def trajectory_viewer_facts(user_id: str, *, client: str | None, sid: str | None) -> dict:
    """Authority facts the trajectory worker cannot read itself.

    ``mobile_session_valid`` applies the rules of ``auth.mobile.validate_claims``
    to the token's client and mobile session; ``admin_enabled`` is the
    TRAJECTORY_ADMIN_* allowlist of this backend.
    """
    async with get_db_session() as db:
        user = await db.get(User, user_id)
        facts = {"user_id": user_id, "role": user.role if user is not None else None,
                 "is_active": bool(user is not None and user.is_active),
                 "is_deleted": bool(user is None or user.is_deleted)}
    try:
        await validate_claims({"sub": user_id, "client": client, "sid": sid})
        mobile_session_valid = True
    except HTTPException:
        mobile_session_valid = False
    return {**facts, "mobile_session_valid": mobile_session_valid, "admin_enabled": admin_enabled(user_id)}


@router.post("/trajectory/viewer", dependencies=[Depends(internal_token)])
async def trajectory_viewer(query: TrajectoryViewerQuery) -> dict:
    return await trajectory_viewer_facts(query.user_id, client=query.client, sid=query.sid)


def _clip(value: str | None, width: int) -> str | None:
    return value[:width] if isinstance(value, str) else value


class TrajectoryAuditEntry(BaseModel):
    """One worker audit fact; ``id`` becomes the ``audit_logs`` id, so redelivery is harmless."""
    id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    workspace_id: str | None = Field(default=None, max_length=64)
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    details: dict | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    created_at: datetime

    @field_validator("action")
    @classmethod
    def known_action(cls, value: str) -> str:
        if value not in TRAJECTORY_AUDIT_ACTIONS:
            raise ValueError("Unsupported trajectory audit action")
        return value

    @model_validator(mode="after")
    def fit_columns(self) -> TrajectoryAuditEntry:
        # Oversized request metadata is truncated, never a reason to lose the fact.
        self.resource_type = _clip(self.resource_type, 32)
        self.resource_id = _clip(self.resource_id, 128)
        self.ip_address = _clip(self.ip_address, 45)
        self.user_agent = _clip(self.user_agent, 512)
        if self.created_at.tzinfo is None:
            self.created_at = self.created_at.replace(tzinfo=timezone.utc)
        return self


class TrajectoryAuditBatch(BaseModel):
    entries: list[TrajectoryAuditEntry] = Field(max_length=500)


async def _insert_audit(entries: list[TrajectoryAuditEntry]) -> int:
    async with get_db_session() as db:
        existing = set((await db.scalars(select(AuditLog.id).where(
            AuditLog.id.in_([entry.id for entry in entries])))).all())
        written = 0
        for entry in entries:
            if entry.id in existing:
                continue
            existing.add(entry.id)
            db.add(AuditLog(id=entry.id, user_id=entry.user_id, workspace_id=entry.workspace_id,
                            action=entry.action, resource_type=entry.resource_type,
                            resource_id=entry.resource_id, details=entry.details,
                            ip_address=entry.ip_address, user_agent=entry.user_agent,
                            created_at=entry.created_at))
            written += 1
    return written


async def write_trajectory_audit(entries: list[TrajectoryAuditEntry]) -> int:
    """Write delivered worker audit facts once each; returns the number of new rows."""
    if not entries:
        return 0
    try:
        return await _insert_audit(entries)
    except IntegrityError:
        # An entry the database refuses (an actor that no longer exists) must
        # not keep the rest of its batch out of the audit log.
        written = 0
        for entry in entries:
            try:
                written += await _insert_audit([entry])
            except IntegrityError:
                log.warning("Dropped trajectory audit entry id=%s action=%s", entry.id, entry.action)
        return written


@router.post("/trajectory/audit", dependencies=[Depends(internal_token)])
async def trajectory_audit(batch: TrajectoryAuditBatch) -> dict:
    written = await write_trajectory_audit(batch.entries)
    return {"accepted": len(batch.entries), "written": written}
