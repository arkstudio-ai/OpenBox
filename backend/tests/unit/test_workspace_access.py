"""Workspace membership, invitation, and read-only collaboration coverage."""
from datetime import datetime, timedelta, timezone
import hashlib
import uuid

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api import sessions as session_api
from api import workspaces as workspace_api
from auth.workspace import get_workspace
from core.identifier import generate_id
from db.base import get_db_session
from db.models.workspace import WorkspaceInvitation
from db.repository.user_repo import PgUserRepo
from session import session as session_service


def _request(workspace_id: str | None = None) -> Request:
    headers = []
    if workspace_id:
        headers.append((b"x-workspace-id", workspace_id.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": ("127.0.0.1", 4242),
        }
    )


async def _user(label: str) -> dict:
    suffix = uuid.uuid4().hex[:10]
    return await PgUserRepo().create(
        id=f"workspace_{label}_{suffix}",
        username=f"{label}-{suffix}",
        email=f"{label}-{suffix}@example.test",
        password_hash="unused",
    )


async def test_default_selection_forbidden_selection_and_read_only_invitation(monkeypatch):
    owner = await _user("owner")
    member = await _user("member")
    owner_workspace = owner["default_workspace_id"]

    selected_default = await get_workspace(_request(), {"user_id": member["id"]})
    assert selected_default["id"] == member["default_workspace_id"]

    with pytest.raises(HTTPException) as forbidden:
        await get_workspace(
            _request(owner_workspace),
            {"user_id": member["id"]},
        )
    assert forbidden.value.status_code == 403
    assert forbidden.value.detail["code"] == "WORKSPACE_FORBIDDEN"

    async def no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workspace_api, "record", no_audit)
    invitation = await workspace_api.invite_member(
        owner_workspace,
        workspace_api.InviteBody(target=member["username"], role="member"),
        _request(owner_workspace),
        {"user_id": owner["id"]},
        {"id": owner_workspace, "role": "owner"},
    )
    accepted = await workspace_api.accept_invitation(
        invitation["token"],
        _request(),
        {"user_id": member["id"]},
    )
    assert accepted == {"ok": True, "workspace_id": owner_workspace}
    assert (await get_workspace(
        _request(owner_workspace), {"user_id": member["id"]}
    ))["role"] == "member"

    session = await session_service.create_session(
        title="Owner conversation",
        user_id=owner["id"],
        workspace_id=owner_workspace,
    )
    listed = await session_api.list_sessions(
        current_user={"user_id": member["id"], "workspace_id": owner_workspace}
    )
    assert [item["id"] for item in listed] == [session.id]
    assert listed[0]["owner_username"] == owner["username"]

    with pytest.raises(HTTPException) as read_only:
        await session_api._require_session_owned(
            session.id,
            {"user_id": member["id"], "workspace_id": owner_workspace},
        )
    assert read_only.value.status_code == 403
    assert read_only.value.detail["code"] == "SESSION_READ_ONLY"


async def test_expired_invitation_returns_410(monkeypatch):
    owner = await _user("expired-owner")
    invitee = await _user("expired-member")
    token = "expired-token"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            WorkspaceInvitation(
                id=generate_id(),
                workspace_id=owner["default_workspace_id"],
                target=invitee["username"],
                role="member",
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=now - timedelta(seconds=1),
                created_by=owner["id"],
                created_at=now - timedelta(days=8),
            )
        )

    async def no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workspace_api, "record", no_audit)
    with pytest.raises(HTTPException) as expired:
        await workspace_api.accept_invitation(
            token, _request(), {"user_id": invitee["id"]}
        )
    assert expired.value.status_code == 410
    assert expired.value.detail["code"] == "INVITATION_EXPIRED"
