"""Real SQL and loopback HTTP authority around verification's remote work.

The local Action Server fixture returns command outputs; it never executes a
shell or browser. Actual SandboxClient, runtime/browser orchestration, leases,
diagnostic collection and conditional SQL writes remain in use.
"""
import asyncio
import json
import socket
from types import SimpleNamespace

from fastapi import Request
import pytest
from sqlalchemy import select

from db.base import get_db_session, get_engine
from db.models.desktop_event import DesktopEvent
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from sandbox import channel, browser_runtime
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_terminal_channel_access import target, revoke  # noqa: F401


async def test_expired_verify_cannot_overwrite_a_committed_revoke(target):
    await revoke(target)
    with pytest.raises(channel.ChannelNotReady):
        await channel.wuying_channel.verify(target.record, timeout_sec=0)
    current = await cloud_desktop_repo.get(target.record["id"])
    assert current["tunnel_state"] == "revoked"
    assert current["channel_error"] is None


@pytest.fixture
async def remote(target):
    data = SimpleNamespace(pause=None, reached=asyncio.Event(), release=asyncio.Event(),
                           calls=[], counts={}, runtime_bad=False, browser_bad=False, task=None)
    data.release.set()
    chrome = {"Browser": "HeadlessChrome/151", "webSocketDebuggerUrl": "ws://fixture.invalid/cdp"}
    healthy = json.dumps({"version": browser_runtime.RUNTIME_VERSION, "ready": True})

    async def authenticated(request):
        stored = await cloud_desktop_repo.get(target.record["id"])
        assert channel.action_key_hash(request.headers["x-api-key"]) == stored["action_api_key_hash"]

    async def step(kind):
        data.counts[kind] = data.counts.get(kind, 0) + 1
        label = f"{kind}{data.counts[kind]}"
        data.calls.append(label)
        if label == data.pause:
            data.reached.set()
            await data.release.wait()

    @target.remote_app.get("/alive")
    async def alive():
        await step("alive")
        return {"status": "ok"}

    @target.remote_app.post("/desktop/lease/{operation}")
    async def lease(operation: str, request: Request):
        await authenticated(request)
        await step(operation)
        return {"token": "verify-lease-fixture", "wait_ms": 0}

    @target.remote_app.post("/execute")
    async def execute(request: Request):
        await authenticated(request)
        command = (await request.json())["command"]
        code, output = 0, ""
        if "hostname;" in command:
            kind, output = "display", "fixture-host\nOPENBOX_NO_DISPLAY\n"
        elif command.endswith("repair_browser_runtime.py --check"):
            kind = "runtime"
            output = '{"version":"old","ready":true}' if data.runtime_bad else healthy
        elif command.startswith("curl "):
            if ":9333" in command:
                kind, output = "chrome", json.dumps(chrome)
            else:
                kind = "relay"
                output = json.dumps({"configuredMode": "local", "chromeAvailable": not data.browser_bad})
        elif command.startswith(": obx-chrome-probe;"):
            kind, output = "renderer", json.dumps(chrome)
        elif command.startswith(": obx-diag;"):
            kind, output = "diag", json.dumps({"diag_version": "fixture", "summary": {}})
        else:
            # Only the existing runtime install script is accepted here, and
            # returned as successful fixture data without executing it.
            assert "--register-service" in command
            kind, output = "runtime_repair", healthy
        await step(kind)
        return {"exit_code": code, "stdout": output, "stderr": ""}

    async def start(pause=None):
        data.pause = pause
        if pause:
            data.release.clear()
        data.task = asyncio.create_task(channel.wuying_channel.verify(target.record, timeout_sec=2))
        if pause:
            await asyncio.wait_for(data.reached.wait(), 3)
            assert get_engine().sync_engine.pool.checkedout() == 0
        return data.task

    data.start = start
    try:
        yield data
    finally:
        data.release.set()
        if data.task:
            await asyncio.wait_for(asyncio.gather(data.task, return_exceptions=True), 5)


@pytest.mark.parametrize("pause", ["alive1", "acquire1", "display1", "runtime1", "runtime2", "chrome1", "renderer1", "relay1"])
async def test_revocation_during_real_http_stops_all_following_actions(target, remote, pause):
    task = await remote.start(pause)
    await revoke(target)
    before = len(remote.calls)
    remote.release.set()
    with pytest.raises(channel.ChannelNotReady, match="original binding"):
        await asyncio.wait_for(task, 3)
    assert all(label.startswith("release") for label in remote.calls[before:])
    if pause not in ("alive1",):
        assert "release1" in remote.calls
    current = await cloud_desktop_repo.get(target.record["id"])
    assert current["tunnel_state"] == "revoked" and current["channel_error"] is None
    async with get_db_session() as db:
        statuses = await db.scalars(select(DesktopEvent.status).where(
            DesktopEvent.desktop_id == target.record["desktop_id"], DesktopEvent.kind == "channel.verify"))
        assert list(statuses) == ["fail"]


@pytest.mark.parametrize("change", ["workspace_id", "desktop_id", "assigned_at", "action_api_key_ciphertext"])
async def test_successful_old_browser_result_cannot_update_replacement_binding(target, remote, change):
    from datetime import timedelta
    task = await remote.start("relay1")
    replacement = {
        "workspace_id": None, "desktop_id": "replacement-" + target.workspace,
        "assigned_at": target.record["assigned_at"] + timedelta(seconds=1),
        "action_api_key_ciphertext": channel.encrypt_action_key("terminal-key-fixture"),
    }[change]
    await cloud_desktop_repo.update(target.record["id"], **{change: replacement}, channel_error="new binding")
    # Mutating the caller's dict cannot change the verification's snapshot.
    target.record[change] = replacement
    remote.release.set()
    with pytest.raises(channel.ChannelNotReady, match="original binding"):
        await asyncio.wait_for(task, 3)
    current = await cloud_desktop_repo.get(target.record["id"])
    assert current["channel_error"] == "new binding" and current["last_seen_at"] is None


@pytest.mark.parametrize("initial", ["pending", "up", "down"])
async def test_normal_verification_preserves_real_browser_checks_and_readiness_report(target, remote, initial):
    await cloud_desktop_repo.update(target.record["id"], tunnel_state=initial)
    target.record = await cloud_desktop_repo.get(target.record["id"])
    if initial == "up":
        from datetime import datetime, timedelta, timezone
        task = await remote.start("relay1")
        await cloud_desktop_repo.update(target.record["id"],
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            charge_type="PrePaid", last_seen_at=datetime.now(timezone.utc), status="starting")
        remote.release.set()
    else:
        task = await remote.start()
    result = await asyncio.wait_for(task, 4)
    assert result["hostname"] == "fixture-host" and result["display_ready"] is False
    assert result["browser_presentation"] == "headless"
    assert {"runtime1", "runtime2", "chrome1", "renderer1", "relay1"} <= set(remote.calls)
    assert remote.counts["acquire"] == remote.counts["release"] == 2
    current = await cloud_desktop_repo.get(target.record["id"])
    assert current["tunnel_state"] == "up" and current["last_seen_at"] is not None
    assert get_engine().sync_engine.pool.checkedout() == 0


@pytest.mark.parametrize("pause", ["runtime1", "runtime_repair1", "diag1"])
async def test_real_repair_and_diagnostic_helpers_cannot_continue_after_revoke(target, remote, pause):
    remote.runtime_bad = pause.startswith("runtime")
    remote.browser_bad = pause == "diag1"
    task = await remote.start(pause)
    await revoke(target)
    before = len(remote.calls)
    remote.release.set()
    with pytest.raises(channel.ChannelNotReady, match="original binding"):
        await asyncio.wait_for(task, 3)
    assert all(label.startswith("release") for label in remote.calls[before:])
    current = await cloud_desktop_repo.get(target.record["id"])
    assert current["tunnel_state"] == "revoked" and current["channel_error"] is None


@pytest.mark.parametrize("pause", ["check", "chunk"])
async def test_cloud_repair_composite_stops_after_inflight_command_returns(target, monkeypatch, pause):
    # A closed loopback port produces a real ConnectError. Cloud Assistant
    # command delivery is the explicit fake boundary; no command is executed.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    await cloud_desktop_repo.update(target.record["id"], tunnel_port=port)
    target.record = await cloud_desktop_repo.get(target.record["id"])
    calls, waiting, release = [], asyncio.Event(), asyncio.Event()

    async def command(_desktop_id, script, timeout):
        if script == "systemctl disable --now openbox-tunnel":
            raise RuntimeError("fixture cloud stop unavailable")
        label = "check" if script.endswith(" --check") else "chunk"
        calls.append(label)
        if label == pause:
            waiting.set()
            await release.wait()
        if label == "check":
            raise RuntimeError("fixture runtime check failed")
        return "fixture chunk accepted"

    monkeypatch.setattr(channel, "run_desktop_command", command)
    task = asyncio.create_task(channel.wuying_channel.verify(target.record, timeout_sec=2))
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        assert get_engine().sync_engine.pool.checkedout() == 0
        await channel.wuying_channel.revoke(target.record)
        release.set()
        with pytest.raises(channel.ChannelNotReady, match="original binding"):
            await asyncio.wait_for(task, 3)
        assert calls == (["check"] if pause == "check" else ["check", "chunk"])
        assert (await cloud_desktop_repo.get(target.record["id"]))["tunnel_state"] == "revoked"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        sock.close()


@pytest.fixture
def cloud_boundary(target, monkeypatch):
    """Keep real lifecycle/install/verify; replace only ECD command delivery."""
    from sandbox import wuying_ecd, wuying_desktop_service
    calls = []
    target.config.wuying_channel = "ssh"
    target.config.wuying_relay_host = "fixture-relay.invalid"
    target.config.wuying_relay_user = "fixture"
    target.config.wuying_relay_hostkey = "fixture-host-key"
    target.config.wuying_tunnel_bind = "127.0.0.1"
    monkeypatch.setattr(wuying_desktop_service, "get_config", lambda: target.config)

    async def command(desktop_id, script, timeout=300):
        calls.append("stop" if script == "systemctl disable --now openbox-tunnel" else "command")
        if calls[-1] == "stop":
            raise RuntimeError("fixture cloud stop unavailable")
        if script.endswith(" --check"):
            return json.dumps({"version": browser_runtime.RUNTIME_VERSION, "ready": True})
        assert "OPENBOX_PUBKEY=" in script
        return ("OPENBOX_PUBKEY=ssh-ed25519 fixture-public-key\nOPENBOX_FINGERPRINT=SHA256:"
                + target.workspace + "x" * 18)

    async def create(*_args, **_kwargs):
        calls.append("create")
        return target.record["desktop_id"]

    async def describe(*_args, **_kwargs):
        calls.append("describe")
        return {"status": "Running"}

    async def end_user(*_args, **_kwargs):
        calls.append("end_user")
        return "fixture-end-user", False

    def boundary(name):
        async def run(*_args, **_kwargs):
            calls.append(name)
        return run

    monkeypatch.setattr(channel, "run_desktop_command", command)
    monkeypatch.setattr(wuying_ecd, "create_desktop", create)
    monkeypatch.setattr(wuying_ecd, "describe_desktop", describe)
    monkeypatch.setattr(wuying_ecd, "ensure_end_user", end_user)
    for name in ("start_desktop", "wait_desktop_ready", "modify_entitlement", "tag_desktop", "untag_desktop"):
        monkeypatch.setattr(wuying_ecd, name, boundary(name))
    return calls


async def lifecycle(target, flow):
    from sandbox.wuying_desktop_service import WuyingDesktopService
    service = WuyingDesktopService()
    if flow == "create":
        return await service._create_flow(target.workspace, target.record["id"], "fixture")
    if flow == "start":
        return await service._start_flow(target.workspace, target.record["id"], target.record["desktop_id"])
    if flow == "channel":
        return await service._channel_flow(target.workspace, target.record["id"])
    from sandbox.pool import pool_service
    return await pool_service.assign_claimed(target.record, target.workspace, target.owner)


@pytest.mark.parametrize("flow", ["create", "start", "channel", "pool"])
async def test_actual_lifecycle_stops_without_old_binding_failure_writes_or_compensation(
    target, remote, cloud_boundary, flow,
):
    if flow == "pool":
        await cloud_desktop_repo.update(target.record["id"], pool_state="assigning")
        target.record = await cloud_desktop_repo.get(target.record["id"])
    remote.pause = "relay1"
    remote.release.clear()
    task = asyncio.create_task(lifecycle(target, flow))
    try:
        await asyncio.wait_for(remote.reached.wait(), 4)
        await channel.wuying_channel.revoke(target.record)
        await cloud_desktop_repo.update(target.record["id"], workspace_id=None,
                                       pool_state="released", channel_error="replacement binding")
        after_revoke = list(cloud_boundary)
        remote.release.set()
        if flow == "pool":
            with pytest.raises(channel.ChannelVerificationStopped):
                await asyncio.wait_for(task, 4)
        else:
            await asyncio.wait_for(task, 4)
        assert cloud_boundary == after_revoke  # No revoke-latest, entitlement or tag compensation.
        current = await cloud_desktop_repo.get(target.record["id"])
        assert current["tunnel_state"] == "revoked" and current["workspace_id"] is None
        assert current["pool_state"] == "released" and current["channel_error"] == "replacement binding"
    finally:
        remote.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("flow", ["create", "start"])
async def test_current_browser_failure_keeps_lifecycle_recovery_state(target, remote, cloud_boundary, flow):
    remote.browser_bad = True
    await asyncio.wait_for(lifecycle(target, flow), 4)
    current = await cloud_desktop_repo.get(target.record["id"])
    assert current["status"] == "starting" and current["tunnel_state"] == "down"
    assert "did not pass readiness" in current["channel_error"]


async def test_revoke_after_verify_failure_but_before_outer_catch_write_stays_revoked(
    target, remote, cloud_boundary, monkeypatch,
):
    remote.browser_bad = True
    waiting, release = asyncio.Event(), asyncio.Event()
    original = cloud_desktop_repo.write_channel_attempt

    async def delayed(expected, fields, **kwargs):
        if fields.get("status") == "starting" and fields.get("tunnel_state") == "down":
            waiting.set()
            await release.wait()
        return await original(expected, fields, **kwargs)

    monkeypatch.setattr(cloud_desktop_repo, "write_channel_attempt", delayed)
    task = asyncio.create_task(lifecycle(target, "start"))
    try:
        await asyncio.wait_for(waiting.wait(), 4)
        await channel.wuying_channel.revoke(target.record)
        await cloud_desktop_repo.update(target.record["id"], channel_error="new owner state")
        release.set()
        await asyncio.wait_for(task, 4)
        current = await cloud_desktop_repo.get(target.record["id"])
        assert current["tunnel_state"] == "revoked" and current["channel_error"] == "new owner state"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("flow", ["create", "start"])
async def test_partial_install_failure_resumes_same_desktop_through_status(
    target, remote, cloud_boundary, monkeypatch, flow,
):
    """The install attempt records partial failure and retries the same key.

    The real next status/resync reinstalls and verifies that same row. Cloud
    delivery is a local fixture; this does not prove a physical guest repair.
    """
    from sandbox import entitlement, wuying_desktop_service
    monkeypatch.setattr(entitlement, "subscription_sandbox_enabled", lambda: False)
    monkeypatch.setattr(wuying_desktop_service, "get_config", lambda: target.config)
    initial = "creating" if flow == "create" else "starting"
    await cloud_desktop_repo.update(target.record["id"], status=initial,
        action_api_key_ciphertext=None, action_api_key_hash=None)
    command = channel.run_desktop_command

    async def fail_install(desktop_id, script, timeout=300):
        if "OPENBOX_PUBKEY=" in script:
            cloud_boundary.append("install_failed")
            raise RuntimeError("fixture install delivery failed after key persistence")
        return await command(desktop_id, script, timeout=timeout)

    monkeypatch.setattr(channel, "run_desktop_command", fail_install)
    await asyncio.wait_for(lifecycle(target, flow), 4)
    pending = await cloud_desktop_repo.get(target.record["id"])
    assert pending["status"] == "starting" and pending["tunnel_state"] == "down"
    assert pending["action_api_key_ciphertext"] is not None
    assert "fixture install delivery failed" in pending["channel_error"]
    monkeypatch.setattr(channel, "run_desktop_command", command)
    calls_before_status = list(cloud_boundary)
    service = wuying_desktop_service.WuyingDesktopService()
    remote.pause, remote.browser_bad = "alive1", True
    remote.release.clear()
    recovery = None
    try:
        response = await service.status(target.workspace)
        recovery = service._inflight[target.workspace]
        await asyncio.wait_for(remote.reached.wait(), 4)
        assert response["desktopId"] == target.record["desktop_id"]
        assert response["state"] == "starting" and response["channel"]["state"] == "down"
        assert cloud_boundary == calls_before_status + ["describe", "command", "command"]
        remote.release.set()
        await asyncio.wait_for(recovery, 4)
        current = await cloud_desktop_repo.get_for_workspace(target.workspace)
        assert current["id"] == target.record["id"]
        assert current["desktop_id"] == target.record["desktop_id"]
        assert current["status"] == "starting" and current["tunnel_state"] == "down"
        assert "did not pass readiness" in current["channel_error"]
        assert service._payload(current)["channel"]["error"] == current["channel_error"]
        assert cloud_boundary == calls_before_status + ["describe", "command", "command"]
    finally:
        remote.release.set()
        if recovery is not None:
            await asyncio.wait_for(asyncio.gather(recovery, return_exceptions=True), 5)
