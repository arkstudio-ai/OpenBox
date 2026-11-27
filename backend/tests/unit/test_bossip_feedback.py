from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from api import admin, sessions as session_routes
from assistant.service import ensure_main_session
from auth.contact import verified_phone
from auth.middleware import get_current_user
from db.base import get_db_session
from db.models.audit_log import AuditLog
from db.models.file_asset import FileAsset
from db.models.workspace import WorkspaceMember
from db.repository.user_repo import PgUserRepo
from light import dish_import, production_form
from light.mode import LightError, set_ui_mode, ui_mode
from session import session as sessions


async def _account(*, role="user", **kwargs):
    identity = uuid4().hex
    return await PgUserRepo().create(
        id=identity, username=identity, password_hash=None, role=role, **kwargs,
    )


def _client(router, identity, workspace, prefix=""):
    app = FastAPI()
    app.include_router(router, prefix=prefix)
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": identity["id"], "role": identity["role"],
    }
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                             headers={"X-Workspace-Id": workspace})


async def test_batch_delete_is_partial_and_preserves_shared_and_fixed_assistant():
    owner, peer = await _account(), await _account()
    workspace = owner["default_workspace_id"]
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(WorkspaceMember(workspace_id=workspace, user_id=peer["id"], role="member",
                               status="active", created_at=now, updated_at=now))
    first = await sessions.create_session(user_id=owner["id"], workspace_id=workspace)
    second = await sessions.create_session(user_id=owner["id"], workspace_id=workspace)
    shared = await sessions.create_session(user_id=peer["id"], workspace_id=workspace)
    assistant = await ensure_main_session(user_id=owner["id"], workspace_id=workspace)

    async with _client(session_routes.router, owner, workspace, "/api/agent") as client:
        response = await client.post("/api/agent/session/batch-delete", json={
            "session_ids": [first.id, shared.id, second.id, assistant.id, first.id],
        })
        assert response.status_code == 200, response.text
        assert response.json() == {"deleted": [first.id, second.id], "failed": [shared.id, assistant.id]}
        assert (await client.get(f"/api/agent/session/{first.id}")).status_code == 404
        assert (await client.get(f"/api/agent/session/{shared.id}")).status_code == 200
        assert (await client.get(f"/api/agent/session/{assistant.id}")).status_code == 200
        retry = await client.post("/api/agent/session/batch-delete", json={"session_ids": [first.id, shared.id]})
        assert retry.json() == {"deleted": [first.id], "failed": [shared.id]}


async def test_new_account_mode_persists_and_existing_explicit_preference_wins():
    new = await _account(ui_mode="light")
    legacy = await _account()
    assert await ui_mode(new["id"]) == "light"
    assert await ui_mode(legacy["id"]) == "pro"
    from db.repository.preference_repo import PgPreferenceRepo
    await PgPreferenceRepo().upsert(legacy["id"], extra={"first_video": {"status": "offered"}})
    assert await ui_mode(legacy["id"]) == "light"
    await set_ui_mode(legacy["id"], "pro")
    assert await ui_mode(legacy["id"]) == "pro"
    await set_ui_mode(new["id"], "pro")
    assert await ui_mode(new["id"]) == "pro"


async def test_menu_recognition_is_only_a_preview_and_requires_current_revision(monkeypatch):
    user = await _account()
    workspace = user["default_workspace_id"]
    target = await sessions.create_session(
        user_id=user["id"], workspace_id=workspace, light_entry="menu",
        light_production=production_form.initial_form("menu", "Cafe", ""),
    )
    asset_id = uuid4().hex
    async with get_db_session() as db:
        db.add(FileAsset(id=asset_id, name="menu.jpg", user_id=user["id"], workspace_id=workspace,
                         session_id=target.id, oss_key=asset_id, mime="image/jpeg", size=1024, status="ready",
                         created_at=datetime.now(timezone.utc)))

    mocked = AsyncMock(return_value=dish_import.DishCandidates(dishes=[
        production_form.Dish(name="Cơm gà", price="45.000 ₫"),
        production_form.Dish(name="Trà"),
    ]))
    monkeypatch.setattr(dish_import, "_read_menu", mocked)
    identity = (user["id"], workspace, target.id)
    with pytest.raises(LightError) as conflict:
        await dish_import.recognize(*identity, dish_import.RecognizeDishes(revision=2, asset_ids=[asset_id]))
    assert conflict.value.status == 409
    with pytest.raises(LightError):
        await dish_import.recognize(*identity, dish_import.RecognizeDishes(revision=1, asset_ids=["missing"]))
    mocked.assert_not_awaited()
    result = await dish_import.recognize(*identity, dish_import.RecognizeDishes(revision=1, asset_ids=[asset_id]))
    assert [item.price for item in result.dishes] == ["45.000 ₫", ""]
    saved = await sessions.get_session(target.id, user_id=user["id"])
    assert saved.light_production["dishes"] == []
    assert saved.light_production["revision"] == 1


async def test_admin_exact_search_masks_verified_contacts_and_audits_without_query():
    assert verified_phone({"phone_number": "+65 8123 4567", "phone_number_verified": False}) is None
    number = verified_phone({"phone_number": "6581234567", "phone_number_verified": True})
    target = await _account(phone_number=number, email="Shop@Example.invalid")
    platform_admin = await _account(role="admin")
    async with _client(admin.router, target, target["default_workspace_id"]) as client:
        forbidden = await client.post("/api/admin/users/search", json={"kind": "phone", "q": number})
        assert forbidden.status_code == 403

    async with _client(admin.router, platform_admin, platform_admin["default_workspace_id"]) as client:
        for kind, keyword in [("phone", "+65 (8123) 4567"), ("email", " shop@EXAMPLE.invalid ")]:
            result = await client.post("/api/admin/users/search", json={"kind": kind, "q": keyword})
            assert result.status_code == 200, result.text
            item, = result.json()["items"]
            assert item["id"] == target["id"]
            assert item["phone"] == "•••• 4567"
            assert item["email"] == "S••@Example.invalid"
        missing = await client.post("/api/admin/users/search", json={"kind": "email", "q": "shop"})
        assert missing.json()["total"] == 0

    async with get_db_session() as db:
        records = (await db.scalars(select(AuditLog).where(AuditLog.user_id == platform_admin["id"]))).all()
    assert records
    assert "8123" not in repr([record.__dict__ for record in records])
    assert "shop@EXAMPLE.invalid" not in repr([record.__dict__ for record in records])


async def test_phone_backfill_matches_subject_and_does_not_replace_existing_phone():
    from scripts.backfill_logto_phones import backfill
    legacy = await _account(oauth_provider="logto", oauth_id="old-subject")
    current = await _account(oauth_provider="logto", oauth_id="current-subject", phone_number="+6581230000")
    profiles = [{"id": "old-subject", "primaryPhone": "6581234567"},
                {"id": "current-subject", "primaryPhone": "6581239999"}]
    assert await backfill(profiles, apply=False) == 1
    assert await backfill(profiles, apply=True) == 1
    assert await backfill(profiles, apply=True) == 0
    from db.models.user import User
    async with get_db_session() as db:
        assert (await db.get(User, legacy["id"])).phone_number == "+6581234567"
        assert (await db.get(User, current["id"])).phone_number == "+6581230000"
