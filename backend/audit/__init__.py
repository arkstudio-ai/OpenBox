"""Best-effort audit recording for security and administrative events."""
from fastapi import Request

from core.log import create_logger
from db.repository.audit_repo import PgAuditRepo


log = create_logger("audit")
_repo = PgAuditRepo()


async def record(
    actor_user_id: str,
    workspace_id: str | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict | None = None,
    request: Request | None = None,
) -> None:
    """Persist an audit event without making the primary operation fail."""
    try:
        ip_address = request.client.host if request and request.client else None
        user_agent = request.headers.get("user-agent") if request else None
        await _repo.create(
            user_id=actor_user_id,
            workspace_id=workspace_id,
            action=action,
            resource_type=target_type,
            resource_id=target_id,
            details=detail,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except Exception as exc:
        log.warning(
            "Failed to record audit action=%s actor=%s error_type=%s",
            action,
            actor_user_id,
            type(exc).__name__,
        )
