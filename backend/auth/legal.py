"""Authenticated, server-timestamped records of explicit policy acceptance."""
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from auth.middleware import get_current_user
from db.repository.audit_repo import PgAuditRepo

POLICY_VERSION = "2026-09-28"
router = APIRouter(prefix="/api/auth", tags=["Auth"])
_audit = PgAuditRepo()


class LegalConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["2026-09-28"]
    accepted: Literal[True]
    language: Literal["zh-CN", "en-US"]
    channel: Literal["web", "native"]


@router.post("/me/legal-consent")
async def record_consent(body: LegalConsent, user: dict = Depends(get_current_user)):
    record = await _audit.create(
        user["user_id"], "legal.consent",
        details=body.model_dump(),
    )
    return {"version": body.version, "accepted_at": record["created_at"]}
