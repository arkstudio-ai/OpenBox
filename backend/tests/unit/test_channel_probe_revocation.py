"""Late local HTTP health results cannot undo a committed channel revocation.

The SQL repository and WuyingChannel.probe/revoke run unchanged; only the
external cloud stop IO fails in the shared disposable terminal fixture.
"""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

from fastapi import Request
from fastapi.responses import JSONResponse
import httpx
import pytest
from sqlalchemy import event

from db.base import get_engine
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from sandbox import channel, terminal_channel
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_terminal_channel_access import target, revoke  # noqa: F401


@pytest.fixture
async def probe(target):
    data = SimpleNamespace(started=asyncio.Event(), release=asyncio.Event(), status=200,
                           task=None, snapshot=dict(target.record))

    @target.remote_app.get("/system_info")
    async def system_info(request: Request):
        assert request.headers["x-api-key"] == "terminal-key-fixture"
        data.started.set()
        await data.release.wait()
        return JSONResponse({"ok": data.status == 200}, status_code=data.status)

    async def start():
        data.task = asyncio.create_task(channel.wuying_channel.probe(data.snapshot))
        await asyncio.wait_for(data.started.wait(), 3)
        # The real HTTP response is pending, with no SQL connection held.
        assert get_engine().sync_engine.pool.checkedout() == 0
        return data.task

    data.start = start
    try:
        yield data
    finally:
        data.release.set()
        if data.task is not None:
            await asyncio.wait_for(asyncio.gather(data.task, return_exceptions=True), 3)


@pytest.mark.parametrize("status", [200, 503])
async def test_late_http_success_or_failure_cannot_restore_revoked_channel(target, probe, status):
    probe.status = status
    _, access = await terminal_channel.resolve_terminal_channel(
        target.provider, target.record["desktop_id"], target.workspace)
    task = await probe.start()
    await revoke(target)  # Real committed SQL revoke; cloud stop fails.
    with pytest.raises(PermissionError):
        await access.check()
    probe.release.set()
    assert await task is False
    current = await cloud_desktop_repo.get(target.record["id"])
    assert current["tunnel_state"] == "revoked"
    assert current["last_seen_at"] is None and current["channel_error"] is None
    with pytest.raises(PermissionError):
        await access.check()
    with pytest.raises(PermissionError):
        await terminal_channel.resolve_terminal_channel(
            target.provider, target.record["desktop_id"], target.workspace)


@pytest.mark.parametrize("status", [200, 503])
@pytest.mark.parametrize("field", [
    "desktop_id", "region_id", "workspace_id", "assigned_at", "pool_state",
    "channel_kind", "private_ip", "tunnel_bind", "tunnel_port", "tunnel_fingerprint",
    "tunnel_pubkey", "action_api_key_hash", "action_api_key_ciphertext",
])
async def test_late_probe_cannot_update_new_assignment_route_or_credential(target, probe, status, field):
    probe.status = status
    task = await probe.start()
    value = {
        "desktop_id": "new-" + target.workspace, "region_id": "cn-other-fixture",
        "workspace_id": None, "assigned_at": target.record["assigned_at"] + timedelta(seconds=1),
        "pool_state": "released", "channel_kind": "direct", "private_ip": "127.0.0.2",
        "tunnel_bind": "localhost", "tunnel_port": None,
        "tunnel_fingerprint": "new-" + target.workspace, "tunnel_pubkey": "new-fixture-public-key",
        "action_api_key_hash": "0" * 64,
        "action_api_key_ciphertext": channel.encrypt_action_key("terminal-key-fixture"),
    }[field]
    marker = "new binding health must remain untouched"
    await cloud_desktop_repo.update(target.record["id"], **{field: value}, channel_error=marker)
    probe.release.set()
    assert await task is False
    current = await cloud_desktop_repo.get(target.record["id"])
    assert current["tunnel_state"] == "up" and current["channel_error"] == marker
    assert current["last_seen_at"] is None


async def test_probe_cannot_adopt_a_callers_mutated_snapshot_during_http_wait(target, probe):
    task = await probe.start()
    rotated = channel.encrypt_action_key("terminal-key-fixture")
    await cloud_desktop_repo.update(target.record["id"], action_api_key_ciphertext=rotated)
    probe.snapshot["action_api_key_ciphertext"] = rotated
    probe.release.set()
    assert await task is False
    assert (await cloud_desktop_repo.get(target.record["id"]))["last_seen_at"] is None


@pytest.mark.parametrize("initial", ["pending", "up", "down"])
async def test_current_ssh_channel_still_recovers_with_real_http_and_unrelated_metadata_changes(target, probe, initial):
    await cloud_desktop_repo.update(target.record["id"], tunnel_state=initial, channel_error="old failure")
    probe.snapshot = await cloud_desktop_repo.get(target.record["id"])
    task = await probe.start()
    # Patrol updates these values independently of channel health.
    await cloud_desktop_repo.update(target.record["id"], charge_type="PrePaid", spec="fixture-spec")
    probe.release.set()
    assert await task is True
    current = await cloud_desktop_repo.get(target.record["id"])
    assert current["tunnel_state"] == "up" and current["channel_error"] is None
    assert current["last_seen_at"] is not None and current["spec"] == "fixture-spec"


async def test_current_ssh_failure_still_records_down_with_real_http(target, probe):
    probe.status = 503
    task = await probe.start()
    probe.release.set()
    assert await task is False
    current = await cloud_desktop_repo.get(target.record["id"])
    assert current["tunnel_state"] == "down" and current["channel_error"] == "HTTP 503"


@pytest.mark.parametrize("status", [200, 503])
async def test_direct_channel_keeps_original_endpoint_and_health_transitions(target, monkeypatch, status):
    # Direct channels use fixed port 8000. An in-process HTTP transport avoids
    # binding or touching that possibly occupied application port on this host.
    initial = "down" if status == 200 else "up"
    await cloud_desktop_repo.update(target.record["id"], channel_kind="direct", private_ip="127.0.0.1",
                                   tunnel_state=initial, tunnel_bind=None, tunnel_port=None)
    snapshot = await cloud_desktop_repo.get(target.record["id"])
    client_type = httpx.AsyncClient
    requested = []

    def respond(request):
        requested.append(request)
        return httpx.Response(status, json={"ok": status == 200})

    monkeypatch.setattr(channel.httpx, "AsyncClient",
                        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    assert await channel.wuying_channel.probe(snapshot) is (status == 200)
    assert len(requested) == 1 and str(requested[0].url) == "http://127.0.0.1:8000/system_info"
    assert requested[0].headers["x-api-key"] == "terminal-key-fixture"
    current = await cloud_desktop_repo.get(target.record["id"])
    assert current["tunnel_state"] == ("up" if status == 200 else "down")


async def test_probe_write_is_one_atomic_conditional_update_with_no_pre_read(target):
    engine = get_engine().sync_engine
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.upper())

    event.listen(engine, "before_cursor_execute", observe)
    try:
        assert await cloud_desktop_repo.record_channel_probe(target.record, healthy=True)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert len(statements) == 1 and statements[0].startswith("UPDATE CLOUD_DESKTOPS SET ")
    assert "WHERE " in statements[0] and "CLOUD_DESKTOPS.TUNNEL_STATE =" in statements[0]
    assert get_engine().sync_engine.pool.checkedout() == 0
