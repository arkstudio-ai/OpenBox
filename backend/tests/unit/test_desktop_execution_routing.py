"""Desktop integrations must use the same Wuying route as Agent execution."""
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from core.config import OpenBoxConfig
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from platforms.desktop import service
from sandbox.client import SandboxClient
from sandbox.wuying import WuyingProvider
from trends.service import Caller, run_page


@pytest.fixture
def shared(monkeypatch):
    config = OpenBoxConfig(
        sandbox_provider="wuying", wuying_routing="shared",
        wuying_endpoint="https://desktop.test:8443/action/",
        wuying_desktop_id="ecd-dev", wuying_api_key="private-test-key",
    )
    monkeypatch.setattr("core.config.get_config", lambda: config)
    provider = WuyingProvider()
    monkeypatch.setattr("sandbox.get_provider", lambda: provider)
    # A shared dev desktop does not require a provisioned workspace row, even
    # if an old per-desktop assignment happens to remain in the database.
    lookup = AsyncMock(side_effect=AssertionError("shared execution must not query assignments"))
    monkeypatch.setattr(cloud_desktop_repo, "get_for_workspace", lookup)
    return config, provider


async def test_shared_desktop_uses_active_execution_route_without_exposing_credentials(shared):
    _, provider = shared
    record = await service.workspace_desktop("workspace-a")
    client = service._client_for(record)
    execution = provider.get_user_container("workspace-a")
    assert record["desktop_id"] == execution.name == "ecd-dev"
    assert client.base_url == provider.client_base_url == "https://desktop.test:8443/action"
    assert client.api_key == execution.api_key == "private-test-key"
    assert client.desktop_id == record["desktop_id"]
    assert "private-test-key" not in json.dumps(record)
    assert client.workspace_id is None  # shared dev has no subscription assignment


@pytest.mark.parametrize("routing,provider", [("per_desktop", "wuying"), ("shared", "docker")])
async def test_other_modes_never_fall_back_to_shared_configuration(shared, monkeypatch, routing, provider):
    config, _ = shared
    config.wuying_routing = routing
    config.sandbox_provider = provider
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(cloud_desktop_repo, "get_for_workspace", lookup)
    assert await service.workspace_desktop("workspace-a") is None
    lookup.assert_awaited_once_with("workspace-a")


async def test_per_desktop_preserves_workspace_route_and_channel_readiness(shared, monkeypatch):
    config, _ = shared
    config.wuying_routing = "per_desktop"
    record = {"desktop_id": "ecd-assigned", "workspace_id": "workspace-a", "tunnel_state": "revoked"}
    lookup = AsyncMock(return_value=record)
    monkeypatch.setattr(cloud_desktop_repo, "get_for_workspace", lookup)
    assert await service.workspace_desktop("workspace-a") == record
    with pytest.raises(service.DesktopUnavailable, match="revoked"):
        service._client_for(record)


async def test_shared_record_cannot_survive_a_routing_mode_change(shared):
    config, _ = shared
    record = await service.workspace_desktop("workspace-a")
    config.wuying_routing = "per_desktop"
    with pytest.raises(service.DesktopUnavailable, match="route has changed"):
        service._client_for(record)


async def test_empty_workspace_does_not_acquire_shared_desktop(shared):
    assert await service.workspace_desktop("") is None


@pytest.mark.parametrize("session_id", ["trial-check", "member-check"])
@pytest.mark.parametrize("offline", [False, True])
async def test_trends_reaches_shared_transport_for_trial_and_member(shared, monkeypatch, session_id, offline):
    seen = []

    @asynccontextmanager
    async def lease(client, **kwargs):
        assert kwargs["session_id"] == session_id
        assert kwargs["tool_call_id"] == "tool-check"
        yield

    async def execute(client, command, **kwargs):
        # Do not run Chrome in a unit test: stop at the real transport seam.
        seen.append(client.base_url)
        if offline:
            raise httpx.ConnectError("desktop tunnel offline")
        return SimpleNamespace(exit_code=0, stderr="", stdout='{"ok":true,"value":{"items":[]}}')

    monkeypatch.setattr(SandboxClient, "desktop_lease", lease)
    monkeypatch.setattr(SandboxClient, "execute", execute)
    monkeypatch.setattr("sandbox.events.emit", AsyncMock())
    call = run_page(Caller("workspace-a", "user-a", session_id, "tool-check"),
        url="https://example.test/hot", expression="document.title", wait_resource=None,
        settle_s=0, summary="routing regression")
    if offline:
        with pytest.raises(service.DesktopUnavailable, match="desktop tunnel offline"):
            await call
    else:
        assert (await call)["ok"] is True
    assert seen == ["https://desktop.test:8443/action"]
