"""API keys: issuance, hashing, the middleware branch, and the workspace binding."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from auth import api_key as keys
from auth import middleware, workspace as workspace_dep
from db.base import get_db_session
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember


async def seed_scope(*, member: bool = True, active: bool = True) -> tuple[str, str]:
    """A user with a workspace they own; returns ``(user_id, workspace_id)``."""
    uid, wid = f"u-{uuid4().hex[:10]}", f"w-{uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=uid, username=uid, role="user", is_active=active, failed_login_count=0,
                    created_at=now, updated_at=now))
        await db.flush()
        db.add(Workspace(id=wid, name="ws", owner_user_id=uid, created_at=now, updated_at=now))
        await db.flush()
        if member:
            db.add(WorkspaceMember(workspace_id=wid, user_id=uid, role="owner", status="active",
                                   created_at=now, updated_at=now))
        await db.flush()
        user = await db.get(User, uid)
        user.default_workspace_id = wid
    return uid, wid


def test_secret_shape_and_hash_are_stable():
    secret = keys.generate_secret()
    assert secret.startswith("obx_sk_")
    assert len(secret) > 40
    assert keys.hash_secret(secret) == keys.hash_secret(secret)
    assert keys.hash_secret(secret) != keys.hash_secret(keys.generate_secret())
    assert keys.key_prefix_of(secret) == secret[:15]
    assert keys.is_api_key(secret) and not keys.is_api_key("eyJhbGciOi")


def test_policy_defaults_fill_in_and_scopes_are_validated():
    policy = keys.normalize_policy({"interaction_timeout_s": 600, "allowed_tools": None})
    assert policy["interaction_timeout_s"] == 600
    assert policy["question"] == "auto_reject"
    assert policy["allowed_tools"] == []
    assert keys.validate_scopes(["files:read", "sessions:read", "files:read"]) == ["files:read", "sessions:read"]
    with pytest.raises(ValueError):
        keys.validate_scopes(["admin:*"])


async def test_issue_then_authenticate_returns_member_identity():
    uid, wid = await seed_scope()
    row, secret = await keys.issue_key(user_id=uid, workspace_id=wid, name="test",
                                       scopes=["sessions:read"], policy={"max_concurrent_sessions": 2},
                                       rate_limit="10/minute")
    assert row.key_hash == keys.hash_secret(secret)
    identity = await keys.authenticate(secret)
    assert identity == {
        "user_id": uid, "role": "user", "workspace_id": wid, "workspace_role": "owner",
        "auth_kind": "api_key", "api_key_id": row.id, "scopes": ["sessions:read"],
        "policy": {**keys.DEFAULT_POLICY, "max_concurrent_sessions": 2}, "rate_limit": "10/minute",
    }
    async with get_db_session() as db:
        from db.models.api_key import ApiKey
        stored = await db.get(ApiKey, row.id)
    assert stored.last_used_at is not None
    assert await keys.authenticate("obx_sk_" + "x" * 43) is None
    assert await keys.authenticate("not-a-key") is None


async def test_issue_refuses_non_members():
    uid, wid = await seed_scope(member=False)
    with pytest.raises(LookupError):
        await keys.issue_key(user_id=uid, workspace_id=wid, name="x")


async def test_revoked_expired_and_inactive_keys_fail_closed():
    uid, wid = await seed_scope()
    row, secret = await keys.issue_key(user_id=uid, workspace_id=wid, name="r")
    assert await keys.revoke_key(row.id) is True
    assert await keys.revoke_key(row.id) is False
    assert await keys.authenticate(secret) is None

    _, expired = await keys.issue_key(user_id=uid, workspace_id=wid, name="e",
                                      expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert await keys.authenticate(expired) is None

    _, live = await keys.issue_key(user_id=uid, workspace_id=wid, name="l",
                                   expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    assert (await keys.authenticate(live))["api_key_id"]

    async with get_db_session() as db:
        user = await db.get(User, uid)
        user.is_active = False
    assert await keys.authenticate(live) is None


async def test_membership_loss_revokes_the_key_in_effect():
    uid, wid = await seed_scope()
    _, secret = await keys.issue_key(user_id=uid, workspace_id=wid, name="m")
    async with get_db_session() as db:
        member = await db.get(WorkspaceMember, (wid, uid))
        member.status = "removed"
    assert await keys.authenticate(secret) is None


async def test_middleware_routes_obx_prefix_to_key_auth(monkeypatch):
    monkeypatch.setattr(middleware, "_auth_enabled", True)
    uid, wid = await seed_scope()
    row, secret = await keys.issue_key(user_id=uid, workspace_id=wid, name="mw")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=secret)
    identity = await middleware.get_optional_current_user(SimpleNamespace(), creds)
    assert identity["auth_kind"] == "api_key" and identity["api_key_id"] == row.id

    bad = HTTPAuthorizationCredentials(scheme="Bearer", credentials="obx_sk_nope")
    with pytest.raises(HTTPException) as exc:
        await middleware.get_optional_current_user(SimpleNamespace(), bad)
    assert exc.value.status_code == 401


async def test_workspace_dependency_ignores_header_for_keys():
    uid, wid = await seed_scope()
    row, secret = await keys.issue_key(user_id=uid, workspace_id=wid, name="ws")
    identity = await keys.authenticate(secret)
    request = SimpleNamespace(headers={"X-Workspace-Id": "someone-elses"}, state=SimpleNamespace())
    resolved = await workspace_dep.get_workspace(request, identity)
    assert resolved == {"id": wid, "role": "owner"}
    assert identity["workspace_id"] == wid
