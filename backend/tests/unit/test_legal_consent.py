"""Policy receipts use the authenticated actor and a server timestamp."""
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from auth import legal, middleware


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(middleware, "_auth_enabled", True)
    monkeypatch.setattr(middleware, "_cache", None)
    app = FastAPI()
    app.include_router(legal.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield app, client


def payload(**changes):
    return {"version": legal.POLICY_VERSION, "accepted": True,
            "language": "zh-CN", "channel": "native", **changes}


async def test_anonymous_cannot_create_receipt(client, monkeypatch):
    _, api = client
    audit = AsyncMock()
    monkeypatch.setattr(legal, "_audit", audit)
    assert (await api.post("/api/auth/me/legal-consent", json=payload())).status_code == 401
    audit.create.assert_not_awaited()


@pytest.mark.parametrize("changes", [
    {"accepted": False}, {"version": "old"}, {"channel": "unknown"},
    {"user_id": "someone-else"}, {"accepted_at": "2020-01-01"},
])
async def test_invalid_or_forged_receipt_is_not_stored(client, monkeypatch, changes):
    app, api = client
    app.dependency_overrides[middleware.get_current_user] = lambda: {"user_id": "actor"}
    audit = AsyncMock()
    monkeypatch.setattr(legal, "_audit", audit)
    assert (await api.post("/api/auth/me/legal-consent", json=payload(**changes))).status_code == 422
    audit.create.assert_not_awaited()


@pytest.mark.parametrize("channel", ["web", "native"])
async def test_receipt_records_verified_actor_version_and_server_time(client, monkeypatch, channel):
    app, api = client
    app.dependency_overrides[middleware.get_current_user] = lambda: {"user_id": "actor"}
    audit = AsyncMock()
    audit.create.return_value = {"created_at": "2026-09-28 09:00:00+00:00"}
    monkeypatch.setattr(legal, "_audit", audit)
    body = payload(channel=channel)
    response = await api.post("/api/auth/me/legal-consent", json=body)
    assert response.status_code == 200
    audit.create.assert_awaited_once_with("actor", "legal.consent", details=body)
    assert response.json() == {"version": legal.POLICY_VERSION, "accepted_at": "2026-09-28 09:00:00+00:00"}
