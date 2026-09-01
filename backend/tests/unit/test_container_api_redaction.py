from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from api import containers
from auth import middleware
from auth import quota
from core import config as config_module
from models.container import ContainerInfo, ContainerStatus


class _FakeProvider:
    def __init__(self) -> None:
        self.expose_existing = True
        self.container = ContainerInfo(
            id="wuying-desktop",
            name="desktop",
            status=ContainerStatus.RUNNING,
            image="wuying:test",
            created_at=datetime.now(timezone.utc),
            host="sandbox.internal",
            port=18000,
            api_key="sandbox-secret",
        )

    def get_containers_for_user(self, _user_id: str):
        return [self.container] if self.expose_existing else []

    async def get_container(self, _container_id: str, user_id: str | None = None):
        assert user_id == "user-1"
        return self.container

    async def create_container(
        self,
        _name: str,
        _image: str,
        _project_id: str | None,
        _user_id: str,
    ):
        return self.container


async def test_public_container_endpoints_redact_transport_credentials(monkeypatch):
    fake_provider = _FakeProvider()
    monkeypatch.setattr(containers, "provider", fake_provider)

    async def allow_container(_user_id: str, _config) -> None:
        return None

    monkeypatch.setattr(quota, "check_container_quota", allow_container)
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: SimpleNamespace(
            wuying_desktop_id="ecd-development",
            wuying_endpoint="http://127.0.0.1:18001",
        ),
    )

    app = FastAPI()
    app.include_router(containers.router)
    app.dependency_overrides[middleware.get_current_user] = lambda: {
        "user_id": "user-1",
        "role": "admin",
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        list_response = await client.get("/api/containers")
        get_response = await client.get("/api/containers/wuying-desktop")
        image_status_response = await client.get("/api/containers/sandbox-image/status")
        image_build_response = await client.post("/api/containers/sandbox-image/build")

        fake_provider.expose_existing = False
        create_response = await client.post(
            "/api/containers",
            json={"name": "desktop"},
        )

    assert list_response.status_code == 200
    assert get_response.status_code == 200
    assert create_response.status_code == 201
    assert image_status_response.status_code == 200
    assert image_status_response.json() == {
        "exists": True,
        "image": "wuying:ecd-development",
    }
    assert image_build_response.status_code == 409
    assert "runtime image builds are not supported" in image_build_response.text

    public_payloads = [
        list_response.json()["containers"][0],
        get_response.json(),
        create_response.json(),
    ]
    for payload in public_payloads:
        assert "api_key" not in payload
        assert "host" not in payload
        assert payload["id"] == "wuying-desktop"

    for response in (list_response, get_response, create_response):
        assert "sandbox-secret" not in response.text
        assert "sandbox.internal" not in response.text

    # Providers retain the secret internally for authenticated proxy calls.
    assert fake_provider.container.api_key == "sandbox-secret"
    assert fake_provider.container.host == "sandbox.internal"
