"""Stable asset download links resolve to fresh, owner-bound OSS URLs."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from api import assets as asset_api


@pytest.mark.asyncio
async def test_asset_download_redirects_to_fresh_disposition_url(monkeypatch):
    row = SimpleNamespace(
        id="asset_ready",
        status="ready",
        oss_key="assets/user/asset_ready/final.mp4",
        name="final.mp4",
    )

    class Oss:
        def presign_get(self, key, *, download_name=None):
            assert key == row.oss_key
            assert download_name == row.name
            return "https://oss.example.test/final.mp4?download=fresh"

    @asynccontextmanager
    async def fake_db_session():
        yield object()

    async def fake_owned_asset(_db, asset_id, user_id):
        assert asset_id == row.id
        assert user_id == "user-1"
        return row

    monkeypatch.setattr(asset_api, "_oss_or_503", lambda: Oss())
    monkeypatch.setattr(asset_api, "get_db_session", fake_db_session)
    monkeypatch.setattr(asset_api, "_owned_asset", fake_owned_asset)

    response = await asset_api.asset_download(
        row.id,
        token="",
        current_user={"user_id": "user-1"},
    )

    assert response.status_code == 307
    assert response.headers["location"] == "https://oss.example.test/final.mp4?download=fresh"


@pytest.mark.asyncio
async def test_asset_download_accepts_owner_bound_capability(monkeypatch):
    row = SimpleNamespace(
        id="asset_ready",
        status="ready",
        oss_key="assets/user/asset_ready/final.mp4",
        name="final.mp4",
    )

    class Oss:
        def presign_get(self, key, *, download_name=None):
            return "https://oss.example.test/final.mp4?download=fresh"

    @asynccontextmanager
    async def fake_db_session():
        yield object()

    async def fake_owned_asset(_db, asset_id, user_id):
        assert asset_id == row.id
        assert user_id == "user-1"
        return row

    monkeypatch.setattr(asset_api, "_oss_or_503", lambda: Oss())
    monkeypatch.setattr(asset_api, "get_db_session", fake_db_session)
    monkeypatch.setattr(asset_api, "_owned_asset", fake_owned_asset)
    monkeypatch.setattr(
        asset_api,
        "decode_asset_download_token",
        lambda token, asset_id: {"sub": "user-1"}
        if token == "valid" and asset_id == row.id
        else None,
    )

    response = await asset_api.asset_download(row.id, token="valid", current_user=None)

    assert response.status_code == 307
