"""The shipped portrait catalog round-trips through the real account preference API."""
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from api.assistant_appearance import router
from assistant import appearance
from auth.middleware import get_current_user
from db.repository.preference_repo import PgPreferenceRepo
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


@asynccontextmanager
async def client_for(user_id):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user_id}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_every_new_portrait_persists_without_overwriting_other_preferences():
    owner, other, _ = await accounts()
    await PgPreferenceRepo().upsert(owner, extra={"assistant_voice": "Tina"})
    async with client_for(owner) as client:
        assert (await client.get("/api/assistant/appearance")).json()["preset"] == "ip-01"
        for number in range(1, 35):
            preset = f"ip-{number:02d}"
            response = await client.put("/api/assistant/appearance", json={"preset": preset})
            assert response.status_code == 200, response.text
            assert response.json() == {"enabled": True, "preset": preset, "has_image": False, "image_url": None,
                                       "presets": appearance.preset_catalog()}
            assert (await client.get("/api/assistant/appearance")).json() == response.json()
        response = await client.put("/api/assistant/appearance", json={"enabled": False})
        assert response.json()["preset"] == "ip-34" and response.json()["enabled"] is False
    assert (await PgPreferenceRepo().get(owner))["extra"]["assistant_voice"] == "Tina"
    assert (await appearance.view(other))["preset"] == "ip-01"


@pytest.mark.parametrize("legacy, expected", [("orb", "ip-01"), ("sprout", "ip-10"), ("cat", "ip-12")])
async def test_existing_accounts_and_older_tabs_resolve_to_the_same_new_portrait(legacy, expected):
    owner, _, _ = await accounts()
    await PgPreferenceRepo().upsert(owner, extra={appearance.KEY: {"preset": legacy, "enabled": False}})
    async with client_for(owner) as client:
        assert (await client.get("/api/assistant/appearance")).json()["preset"] == expected
        saved = await client.put("/api/assistant/appearance", json={"preset": legacy})
        assert saved.status_code == 200 and saved.json()["preset"] == expected
        assert saved.json()["enabled"] is False
    assert (await PgPreferenceRepo().get(owner))["extra"][appearance.KEY]["preset"] == expected


async def test_invalid_ids_are_refused_without_losing_the_selected_portrait():
    owner, _, _ = await accounts()
    await appearance.save(owner, {"preset": "ip-29"})
    async with client_for(owner) as client:
        for invalid in ("ip-00", "ip-35", "ip-1", "../avatar.png", "https://example.com/avatar.png", 1, {}):
            response = await client.put("/api/assistant/appearance", json={"preset": invalid})
            assert response.status_code == 422
            assert (await client.get("/api/assistant/appearance")).json()["preset"] == "ip-29"
        assert (await client.put("/api/assistant/appearance", json={"preset": "custom"})).status_code == 422


async def test_custom_portrait_survives_switching_to_and_from_the_builtin_catalog(monkeypatch):
    owner, other, _ = await accounts()
    key = f"assistant-avatars/{owner}/{'a' * 64}.png"
    class Store:
        def presign_get(self, given):
            assert given == key
            return "https://storage.invalid/owned-portrait"
    monkeypatch.setattr(appearance, "get_oss", Store)
    await appearance.save(owner, {"preset": "custom", "image_key": key})
    async with client_for(owner) as client:
        for preset in ("ip-31", "custom"):
            saved = await client.put("/api/assistant/appearance", json={"preset": preset})
            assert saved.status_code == 200
            assert saved.json()["preset"] == preset and saved.json()["has_image"] is True
    # A corrupt preference cannot expose another account's uploaded portrait.
    await PgPreferenceRepo().upsert(other, extra={appearance.KEY: {"preset": "custom", "image_key": key}})
    assert (await appearance.view(other))["image_url"] is None


async def test_unknown_stored_preset_falls_back_without_writing_on_read():
    owner, _, _ = await accounts()
    await PgPreferenceRepo().upsert(owner, extra={appearance.KEY: {"preset": {"old": "value"}}})
    assert (await appearance.view(owner))["preset"] == "ip-01"
    assert (await PgPreferenceRepo().get(owner))["extra"][appearance.KEY]["preset"] == {"old": "value"}
