"""The legacy native SDK credential cannot bypass managed physical control.

ECD is always a local stub. No ticket is requested from an actual desktop.
"""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import select

from api import desktop as routes
from assistant import resource_control as controls
from auth.middleware import get_current_user
from db.base import get_db_session
from db.models.cloud_desktop import CloudDesktop
from db.models.resource_control import ResourceControlLease
from question.runtime import now
from sandbox.wuying_desktop_service import wuying_desktop_service
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


@pytest.fixture
async def native_desktop(monkeypatch):
    user_id, _, workspace_id = await accounts()
    desktop_id, region = "ecd-native-" + workspace_id, "cn-native-test"
    record_id = "native-" + workspace_id
    stamp = now()
    async with get_db_session() as db:
        db.add(CloudDesktop(id=record_id, desktop_id=desktop_id, region_id=region,
            workspace_id=workspace_id, user_id=user_id, status="running", pool_state="assigned",
            created_at=stamp, updated_at=stamp))
    config = SimpleNamespace(sandbox_provider="wuying", wuying_region_id=region,
        wuying_desktop_id=desktop_id, wuying_end_user_id="native-user")
    monkeypatch.setattr(routes, "get_config", lambda: config)
    monkeypatch.setattr(routes, "_per_user", lambda: True)
    target = AsyncMock(return_value=(desktop_id, "native-user"))
    monkeypatch.setattr(wuying_desktop_service, "resolve_ticket_target", target)
    monkeypatch.setattr(wuying_desktop_service, "release_ghost", AsyncMock())
    monkeypatch.setattr("sandbox.entitlement.subscription_sandbox_enabled", lambda: False)
    sdk = SimpleNamespace(get_connection_ticket_async=AsyncMock(return_value=_ticket()))
    def make_client(region_id):
        assert region_id == region
        return sdk
    factory = Mock(side_effect=make_client)
    monkeypatch.setattr(routes, "_ecd_client", factory)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user_id, "workspace_id": workspace_id}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://native.test") as client:
        try:
            yield SimpleNamespace(client=client, sdk=sdk, factory=factory, user_id=user_id,
                workspace_id=workspace_id, desktop_id=desktop_id, record_id=record_id,
                region=region, config=config, target_resolver=target)
        finally:
            # A disposable PostgreSQL DB retains rows between cases. Stop only
            # this fixture's lease so its deliberately expired state cannot be
            # picked up by a later expire_leases worker test.
            async with get_db_session() as db:
                row = await db.scalar(select(ResourceControlLease).where(
                    ResourceControlLease.provider == "wuying", ResourceControlLease.resource_type == "desktop",
                    ResourceControlLease.physical_id == f"{region}:{desktop_id}"))
                if row is not None:
                    row.status, row.admission_state = "hold", "closed"


def _ticket(*, ticket="fixture-native-ticket", task_id=None, status="FINISHED"):
    return SimpleNamespace(body=SimpleNamespace(ticket=ticket, task_id=task_id,
        task_status=status, task_message=None))


async def _enroll(target):
    return await controls.enroll_desktop(desktop_id=target.desktop_id,
        workspace_id=target.workspace_id, user_id=target.user_id)


def _denied(response):
    assert response.status_code == 423
    body = response.json()
    assert body == {"available": False, "reason": "resource_control_required", "code": "RESOURCE_CONTROL_HELD"}
    assert "ticket" not in body and "taskId" not in body


@pytest.mark.parametrize("mode", ["per_user", "shared"])
async def test_unmanaged_desktop_keeps_legacy_ticket_contract(native_desktop, monkeypatch, mode):
    target = native_desktop
    monkeypatch.setattr(routes, "_per_user", lambda: mode == "per_user")
    response = await target.client.get("/api/desktop/ticket")
    assert response.status_code == 200
    assert response.json() == {"ticket": "fixture-native-ticket", "desktopId": target.desktop_id,
        "regionId": target.region}
    assert target.sdk.get_connection_ticket_async.await_count == 1


@pytest.mark.parametrize("state", ["automation_open", "bound", "closed", "expired", "human", "old_assignment"])
async def test_every_enrolled_physical_desktop_refuses_unfenced_native_credentials(native_desktop, state):
    target = native_desktop
    fence = await _enroll(target)
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, fence.resource_id)
        if state == "bound":
            row.remote_journal_id = "a" * 32
        elif state == "closed":
            row.admission_state, row.status = "closed", "draining"
        elif state == "expired":
            row.expires_at = now() - timedelta(seconds=1)
        elif state == "human":
            row.owner_kind, row.owner_id = "human", target.user_id
            row.expires_at = now() + timedelta(seconds=60)
        elif state == "old_assignment":
            desktop = await db.get(CloudDesktop, target.record_id)
            desktop.is_deleted, desktop.pool_state, desktop.workspace_id = True, "released", None
    response = await target.client.get("/api/desktop/ticket", params={"task_id": "old-native-task"})
    _denied(response)
    target.factory.assert_not_called()
    target.sdk.get_connection_ticket_async.assert_not_awaited()
    if state != "old_assignment":
        target.target_resolver.assert_not_awaited()


async def test_shared_ticket_uses_the_same_physical_control_gate(native_desktop, monkeypatch):
    target = native_desktop
    await _enroll(target)
    monkeypatch.setattr(routes, "_per_user", lambda: False)
    _denied(await target.client.get("/api/desktop/ticket"))
    target.sdk.get_connection_ticket_async.assert_not_awaited()


async def test_ecd_returning_a_ticket_after_enrollment_cannot_deliver_it(native_desktop):
    target = native_desktop
    started, finish = asyncio.Event(), asyncio.Event()
    async def late_ticket(_request):
        started.set()
        await finish.wait()
        return _ticket()
    target.sdk.get_connection_ticket_async.side_effect = late_ticket
    request = asyncio.create_task(target.client.get("/api/desktop/ticket"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        # An independent SQL transaction commits while the ECD call is waiting.
        await _enroll(target)
        finish.set()
        _denied(await asyncio.wait_for(request, timeout=5))
    finally:
        finish.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)
    assert target.sdk.get_connection_ticket_async.await_count == 1


async def test_next_ecd_poll_rechecks_control_after_wait(native_desktop, monkeypatch):
    target = native_desktop
    target.sdk.get_connection_ticket_async.side_effect = [
        _ticket(ticket=None, task_id="native-pending", status="PENDING"), _ticket()]
    real_sleep = asyncio.sleep
    paused, continue_poll = asyncio.Event(), asyncio.Event()
    async def between_polls(delay):
        if delay == routes._POLL_INTERVAL:
            paused.set()
            await continue_poll.wait()
        else:
            await real_sleep(delay)
    monkeypatch.setattr(routes.asyncio, "sleep", between_polls)
    request = asyncio.create_task(target.client.get("/api/desktop/ticket"))
    try:
        await asyncio.wait_for(paused.wait(), timeout=5)
        await _enroll(target)
        continue_poll.set()
        _denied(await asyncio.wait_for(request, timeout=5))
    finally:
        continue_poll.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)
    assert target.sdk.get_connection_ticket_async.await_count == 1


async def test_final_ticket_guard_follows_the_subscription_await(native_desktop, monkeypatch):
    target = native_desktop
    async def enrolled_during_entitlement(_workspace):
        assert _workspace == target.workspace_id
        await _enroll(target)
    monkeypatch.setattr("sandbox.entitlement.subscription_sandbox_enabled", lambda: True)
    monkeypatch.setattr("sandbox.entitlement.require_sandbox_subscription", enrolled_during_entitlement)
    _denied(await target.client.get("/api/desktop/ticket"))
    assert target.sdk.get_connection_ticket_async.await_count == 1


async def test_legacy_ghost_cleanup_cannot_mutate_newly_managed_resource(native_desktop):
    target = native_desktop
    async def missing_after_enrollment(_request):
        await _enroll(target)
        raise RuntimeError("InvalidDesktopId.NotFound")
    target.sdk.get_connection_ticket_async.side_effect = missing_after_enrollment
    _denied(await target.client.get("/api/desktop/ticket"))
    wuying_desktop_service.release_ghost.assert_not_awaited()
    async with get_db_session() as db:
        desktop = await db.get(CloudDesktop, target.record_id)
        assert desktop.pool_state == "assigned" and not desktop.is_deleted


async def test_config_region_drift_cannot_hide_the_assigned_physical_control(native_desktop):
    target = native_desktop
    await _enroll(target)
    target.config.wuying_region_id = "another-physical-region"
    _denied(await target.client.get("/api/desktop/ticket"))
    target.target_resolver.assert_not_awaited()
    target.factory.assert_not_called()
