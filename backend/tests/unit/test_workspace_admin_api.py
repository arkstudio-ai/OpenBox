"""Admin workspace endpoints enforce role and remain usable in single-user mode."""
import uuid

import httpx

from auth.middleware import get_current_user
from db.repository.user_repo import PgUserRepo
from main import create_app


async def _request_as(app, identity: dict, path: str) -> httpx.Response:
    async def current_user():
        return dict(identity)

    app.dependency_overrides[get_current_user] = current_user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(path)
    finally:
        app.dependency_overrides.clear()


async def test_admin_endpoints_and_non_admin_403():
    suffix = uuid.uuid4().hex[:10]
    repo = PgUserRepo()
    admin = await repo.create(
        id=f"admin-{suffix}",
        username=f"admin-{suffix}",
        password_hash="unused",
        role="admin",
    )
    user = await repo.create(
        id=f"user-{suffix}",
        username=f"user-{suffix}",
        password_hash="unused",
    )
    app = create_app()

    identity = {"user_id": admin["id"], "role": "admin"}
    users = await _request_as(app, identity, "/api/admin/users")
    workspace = await _request_as(
        app, identity, f"/api/admin/workspaces/{admin['default_workspace_id']}"
    )
    audit = await _request_as(app, identity, "/api/admin/audit")
    assert users.status_code == 200
    assert workspace.status_code == 200
    assert audit.status_code == 200
    assert any(item["id"] == admin["id"] for item in users.json()["items"])
    assert workspace.json()["id"] == admin["default_workspace_id"]

    forbidden = await _request_as(
        app, {"user_id": user["id"], "role": "user"}, "/api/admin/users"
    )
    assert forbidden.status_code == 403


async def test_single_user_mode_can_use_admin_api(monkeypatch):
    import auth.middleware as middleware

    monkeypatch.setattr(middleware, "_auth_enabled", False)
    if await PgUserRepo().get("default") is None:
        await PgUserRepo().create(
            id="default",
            username="default",
            password_hash=None,
            role="admin",
        )
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/admin/users")
    assert response.status_code == 200
