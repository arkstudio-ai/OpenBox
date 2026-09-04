"""Audit records carry request and workspace context."""
import uuid

from sqlalchemy import select
from starlette.requests import Request

from audit import record
from db.base import get_db_session
from db.models.audit_log import AuditLog
from db.repository.user_repo import PgUserRepo


async def test_record_persists_workspace_ip_and_user_agent():
    suffix = uuid.uuid4().hex[:10]
    user = await PgUserRepo().create(
        id=f"audit-user-{suffix}",
        username=f"audit-user-{suffix}",
        password_hash="unused",
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/admin/users",
            "headers": [(b"user-agent", b"workspace-audit-test")],
            "client": ("203.0.113.7", 443),
        }
    )
    await record(
        user["id"],
        user["default_workspace_id"],
        "admin.view_users",
        "user",
        None,
        {"q": ""},
        request,
    )

    async with get_db_session() as db:
        row = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.user_id == user["id"],
                    AuditLog.action == "admin.view_users",
                )
            )
        ).scalar_one()
        assert row.workspace_id == user["default_workspace_id"]
        assert row.ip_address == "203.0.113.7"
        assert row.user_agent == "workspace-audit-test"
        assert row.details == {"q": ""}
