"""Dedicated preview host is an authenticated, fail-closed data plane."""

from datetime import datetime, timezone
from http.cookies import SimpleCookie

import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import auth.jwt as jwt_module
import auth.middleware as auth_middleware
from api import containers
from auth.jwt import create_access_token, init_auth
from auth.preview_origin import (
    ControlPlaneFrameGuardMiddleware,
    PreviewOriginIsolationMiddleware,
)
from auth.preview_token import init_preview_store
from cache.memory_cache import MemoryCache
from core.config import OpenBoxConfig
from models.container import ContainerInfo, ContainerStatus


class _Provider:
    async def get_container(self, container_id: str, user_id: str | None = None):
        return ContainerInfo(
            id=container_id,
            name="preview-test",
            status=ContainerStatus.RUNNING,
            image="test",
            created_at=datetime.now(timezone.utc),
            host="sandbox.internal",
            port=9000,
            api_key="sandbox-key",
        )


@pytest.fixture
def isolated_preview_app(monkeypatch):
    config = OpenBoxConfig(
        jwt_secret="test-preview-jwt-secret",
        preview_public_origin="https://preview.example.test",
        cors_origins=["https://app.example.test"],
        control_public_origins=["https://app.example.test", "https://api.example.test"],
    )
    cache = MemoryCache()
    init_preview_store(cache)

    monkeypatch.setattr(auth_middleware, "_auth_enabled", True)
    monkeypatch.setattr(auth_middleware, "_cache", cache)
    monkeypatch.setattr(jwt_module, "_secret", "")
    monkeypatch.setattr(jwt_module, "_access_expire_minutes", 15)
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(containers, "provider", _Provider())
    init_auth(config.jwt_secret)

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        ControlPlaneFrameGuardMiddleware,
        preview_public_origin=config.preview_public_origin,
    )
    app.add_middleware(
        PreviewOriginIsolationMiddleware,
        preview_public_origin=config.preview_public_origin,
    )
    app.include_router(containers.router)
    app.include_router(containers.preview_router)
    app.include_router(containers.preview_config_router)

    @app.get("/api/control-secret")
    async def control_secret():
        return {"secret": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    yield app, create_access_token("user-1")
    init_preview_store(None)


async def test_preview_host_rejects_control_plane_and_control_host_rejects_preview(
    isolated_preview_app,
):
    app, token = isolated_preview_app
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://preview.example.test",
    ) as preview_client:
        health = await preview_client.get("/health")
        assert health.status_code == 200
        assert "x-frame-options" not in health.headers
        assert (await preview_client.get("/api/control-secret")).status_code == 404
        assert (
            await preview_client.get(
                "/api/preview/config",
                headers={"Authorization": f"Bearer {token}"},
            )
        ).status_code == 404

    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://app.example.test",
    ) as control_client:
        control = await control_client.get("/api/control-secret")
        assert control.status_code == 200
        assert control.headers["x-frame-options"] == "DENY"
        assert control.headers["content-security-policy"] == "frame-ancestors 'none'"
        assert (
            await control_client.get("/api/containers/c1/preview/3000/")
        ).status_code == 404
        config_response = await control_client.get(
            "/api/preview/config",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert config_response.status_code == 200
        assert config_response.headers["cache-control"] == "no-store"
        assert config_response.json() == {
            "mode": "isolated_origin",
            "origin": "https://preview.example.test",
        }


async def test_cross_origin_jwt_post_sets_only_preview_host_cookie(isolated_preview_app):
    app, token = isolated_preview_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://preview.example.test",
    ) as client:
        response = await client.post(
            "/api/containers/c1/preview-token?port=3000",
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "https://app.example.test",
            },
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == "https://app.example.test"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.json() == {
        "url": "https://preview.example.test/api/containers/c1/preview/3000/",
        "mode": "isolated_origin",
    }

    parsed = SimpleCookie()
    parsed.load(response.headers["set-cookie"])
    cookie = parsed[containers.PREVIEW_COOKIE_NAME]
    assert cookie["domain"] == ""
    assert cookie["path"] == "/api/containers/c1/preview/3000"
    assert cookie["httponly"] is True
    assert cookie["secure"] is True
    assert cookie["samesite"].lower() == "none"


async def test_preview_cookie_issuer_requires_jwt_and_exact_control_origin(isolated_preview_app):
    app, token = isolated_preview_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://preview.example.test",
    ) as client:
        missing_jwt = await client.post(
            "/api/containers/c1/preview-token?port=3000",
            headers={"Origin": "https://app.example.test"},
        )
        wrong_origin = await client.post(
            "/api/containers/c1/preview-token?port=3000",
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "https://evil.example.test",
            },
        )
        missing_origin = await client.post(
            "/api/containers/c1/preview-token?port=3000",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert missing_jwt.status_code == 401
    assert wrong_origin.status_code == 403
    assert missing_origin.status_code == 403
