"""Durable install attempt fencing with real SQL and explicit local cloud IO."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, update

from db.base import get_db_session, get_engine
from db.models.desktop_activation import DesktopActivation
from db.models.billing import BillingSubscription
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from sandbox import channel
from sandbox.wuying_ecd import create_desktop as actual_create_desktop
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_terminal_channel_access import target  # noqa: F401
from tests.unit.test_channel_verify_revocation import cloud_boundary, remote  # noqa: F401


@pytest.fixture
def lifecycle_cloud(target, cloud_boundary, monkeypatch):
    """Every ECD boundary, including activation's reads, stays in-process."""
    from sandbox import desktop_activation, entitlement, pool, wuying_ecd
    for module in (desktop_activation, entitlement, pool):
        monkeypatch.setattr(module, "get_config", lambda: target.config)
    physical = target.record["desktop_id"]

    async def create(_workspace, *_args, before_submit=None, **_kwargs):
        if before_submit:
            await before_submit()
        cloud_boundary.append("create")
        return physical

    async def inventory(**_kwargs):
        cloud_boundary.append("list")
        return []

    async def ownership(*_args):
        cloud_boundary.append("ownership")
        return "fixture-end-user"

    async def renew(*_args, **_kwargs):
        cloud_boundary.append("renew")

    monkeypatch.setattr(wuying_ecd, "create_desktop", create)
    monkeypatch.setattr(wuying_ecd, "list_desktops", inventory)
    monkeypatch.setattr(wuying_ecd, "verify_ownership", ownership)
    monkeypatch.setattr(wuying_ecd, "renew_desktop", renew)
    return cloud_boundary


async def activation_job(target):
    stamp = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(DesktopActivation(workspace_id=target.workspace, user_id=target.owner,
            state="queued", step="queued", attempts=0, next_run_at=stamp,
            created_at=stamp, updated_at=stamp))


async def run_lifecycle(target, flow):
    from sandbox.wuying_desktop_service import WuyingDesktopService
    from sandbox.desktop_activation import DesktopActivationService
    from sandbox.pool import PoolService
    service = WuyingDesktopService()
    if flow == "create":
        return await service._create_flow(target.workspace, target.record["id"], "fixture")
    if flow == "start":
        return await service._start_flow(target.workspace, target.record["id"], target.record["desktop_id"])
    if flow == "channel":
        return await service._channel_flow(target.workspace, target.record["id"])
    if flow == "activation":
        return await DesktopActivationService().process(target.workspace)
    return await PoolService().assign_claimed(target.record, target.workspace, target.owner)


async def claimed(target):
    # Unique current expiry makes this fixture's row the eligible newest one
    # even when isolated PostgreSQL retains older cases' rows.
    stamp = datetime.now(timezone.utc)
    await cloud_desktop_repo.update(target.record["id"], workspace_id=None, user_id=None,
        assigned_at=None, pool_state="prewarm", tunnel_state="revoked", charge_type="PrePaid",
        expires_at=stamp + timedelta(days=3650))
    result = await cloud_desktop_repo.claim_prewarm(target.workspace, target.owner,
        usable_until=stamp + timedelta(days=365))
    assert result["id"] == target.record["id"]
    target.record = result
    return result


@pytest.mark.parametrize("pause", ["runtime", "install"])
async def test_late_install_cannot_write_after_committed_revoke(
    target, cloud_boundary, monkeypatch, pause,
):
    waiting, release = asyncio.Event(), asyncio.Event()
    command = channel.run_desktop_command

    async def delayed(desktop_id, script, timeout=300):
        result = await command(desktop_id, script, timeout=timeout)
        kind = "runtime" if script.endswith(" --check") else "install"
        if kind == pause:
            waiting.set()
            await release.wait()
        return result

    monkeypatch.setattr(channel, "run_desktop_command", delayed)
    task = asyncio.create_task(channel.wuying_channel.install(target.record))
    try:
        await asyncio.wait_for(waiting.wait(), 4)
        await channel.wuying_channel.revoke(target.record)
        revoked = await cloud_desktop_repo.get(target.record["id"])
        release.set()
        outcome = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 4))[0]
        current = await cloud_desktop_repo.get(target.record["id"])
        assert current["tunnel_state"] == "revoked"
        for name in ("action_api_key_ciphertext", "tunnel_pubkey", "tunnel_fingerprint"):
            assert current[name] == revoked[name]
        assert isinstance(outcome, channel.ChannelVerificationStopped)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("field", ["workspace_id", "desktop_id", "assigned_at"])
async def test_install_does_not_follow_a_replacement_after_cloud_io(target, cloud_boundary, monkeypatch, field):
    waiting, release = asyncio.Event(), asyncio.Event()
    command = channel.run_desktop_command

    async def delayed(desktop_id, script, timeout=300):
        result = await command(desktop_id, script, timeout=timeout)
        if "OPENBOX_PUBKEY=" in script:
            waiting.set()
            await release.wait()
        return result

    monkeypatch.setattr(channel, "run_desktop_command", delayed)
    task = asyncio.create_task(channel.wuying_channel.install(target.record))
    try:
        await asyncio.wait_for(waiting.wait(), 4)
        replacement = {"workspace_id": None, "desktop_id": "new-" + target.workspace,
            "assigned_at": datetime.now(timezone.utc) + timedelta(days=1)}[field]
        await cloud_desktop_repo.update(target.record["id"], **{field: replacement}, channel_error="successor")
        target.record[field] = replacement  # The caller's dict is not the authority snapshot.
        release.set()
        with pytest.raises(channel.ChannelVerificationStopped):
            await asyncio.wait_for(task, 4)
        saved = await cloud_desktop_repo.get(target.record["id"])
        assert saved["channel_error"] == "successor"
        assert saved["tunnel_pubkey"] == "fixture-public-key"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("change", ["revoke", "new_attempt"])
@pytest.mark.parametrize("flow", ["create", "start", "channel", "pool", "activation"])
async def test_final_lifecycle_write_cannot_overwrite_a_later_revoke(
    target, remote, lifecycle_cloud, monkeypatch, flow, change,
):
    if flow == "pool":
        await claimed(target)
    if flow == "activation":
        await activation_job(target)
    waiting, release = asyncio.Event(), asyncio.Event()
    write = cloud_desktop_repo.write_channel_attempt

    async def delayed(expected, fields, **kwargs):
        if fields.get("status") == "running":
            waiting.set()
            await release.wait()
        return await write(expected, fields, **kwargs)

    monkeypatch.setattr(cloud_desktop_repo, "write_channel_attempt", delayed)
    task = asyncio.create_task(run_lifecycle(target, flow))
    try:
        await asyncio.wait_for(waiting.wait(), 6)
        assert get_engine().sync_engine.pool.checkedout() == 0
        if change == "revoke":
            await channel.wuying_channel.revoke(target.record)
        else:
            await channel.wuying_channel.begin(await cloud_desktop_repo.get(target.record["id"]))
        await cloud_desktop_repo.update(target.record["id"], status="stopped", channel_error="revocation remains")
        before = list(lifecycle_cloud)
        release.set()
        result = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 4))[0]
        if flow == "pool":
            assert isinstance(result, channel.ChannelVerificationStopped)
        else:
            assert not isinstance(result, BaseException)
        current = await cloud_desktop_repo.get(target.record["id"])
        assert current["tunnel_state"] == ("revoked" if change == "revoke" else "up")
        assert current["status"] == "stopped"
        assert current["channel_error"] == "revocation remains"
        assert lifecycle_cloud == before
        if flow == "pool":
            assert current["pool_state"] == "assigning"
        if flow == "activation":
            async with get_db_session() as db:
                job = await db.get(DesktopActivation, target.workspace)
                assert job.state == "needs_attention"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("flow", ["create", "start", "channel", "pool", "activation"])
async def test_normal_lifecycle_completes_real_install_and_verification(target, remote, lifecycle_cloud, flow):
    if flow == "pool":
        await claimed(target)
    else:
        await cloud_desktop_repo.update(target.record["id"], status="starting", tunnel_state="pending",
            action_api_key_ciphertext=None, action_api_key_hash=None,
            **({"desktop_id": None} if flow == "create" else {}))
        target.record = await cloud_desktop_repo.get(target.record["id"])
    if flow == "activation":
        await activation_job(target)
    await asyncio.wait_for(run_lifecycle(target, flow), 6)
    current = await cloud_desktop_repo.get(target.record["id"])
    assert current["status"] == "running" and current["tunnel_state"] == "up"
    assert current["pool_state"] == "assigned" and current["channel_attempt_id"]
    assert current["channel_enrollment_grant"] is None
    assert "relay1" in remote.calls
    assert remote.counts["acquire"] == remote.counts["release"] == 2
    if flow == "activation":
        async with get_db_session() as db:
            assert (await db.get(DesktopActivation, target.workspace)).state == "ready"


async def test_revoked_activation_retries_never_reenroll_or_call_cloud(target, remote, lifecycle_cloud):
    from sandbox.desktop_activation import DesktopActivationService
    await activation_job(target)
    await channel.wuying_channel.revoke(target.record)
    before = list(lifecycle_cloud)
    service = DesktopActivationService()
    for _ in range(2):
        async with get_db_session() as db:
            job = await db.get(DesktopActivation, target.workspace)
            job.next_run_at = datetime.now(timezone.utc)
        assert await service.process(target.workspace) is False
    assert lifecycle_cloud == before and remote.calls == []
    current = await cloud_desktop_repo.get(target.record["id"])
    assert current["tunnel_state"] == "revoked" and current["channel_enrollment_grant"] is None


async def test_claim_grant_is_one_use_and_revoke_closes_it(target, cloud_boundary):
    original = await claimed(target)
    assert original["channel_enrollment_grant"]
    first = await channel.wuying_channel.begin(original)
    assert first.record["tunnel_state"] == "pending"
    assert first.record["channel_enrollment_grant"] is None
    with pytest.raises(channel.ChannelVerificationStopped):
        await channel.wuying_channel.begin(original)
    await channel.wuying_channel.revoke(first.record)
    with pytest.raises(channel.ChannelVerificationStopped):
        await channel.wuying_channel.begin(await cloud_desktop_repo.get(original["id"]))


async def test_interrupted_live_attempt_can_be_recovered_after_service_restart(target, remote, cloud_boundary, monkeypatch):
    from sandbox.wuying_desktop_service import WuyingDesktopService
    command = channel.run_desktop_command

    async def fail(desktop_id, script, timeout=300):
        if "OPENBOX_PUBKEY=" in script:
            raise RuntimeError("fixture delivery unavailable after credential write")
        return await command(desktop_id, script, timeout=timeout)

    monkeypatch.setattr(channel, "run_desktop_command", fail)
    await WuyingDesktopService()._create_flow(target.workspace, target.record["id"], None)
    failed = await cloud_desktop_repo.get(target.record["id"])
    assert failed["tunnel_state"] == "down" and "delivery unavailable" in failed["channel_error"]
    monkeypatch.setattr(channel, "run_desktop_command", command)
    # A new service reconstructs authority from the same durable record. It
    # reinstalls the known key, rather than assuming the partial install worked.
    await WuyingDesktopService()._channel_flow(target.workspace, failed["id"])
    recovered = await cloud_desktop_repo.get(failed["id"])
    assert recovered["status"] == "running" and recovered["tunnel_state"] == "up"
    assert recovered["desktop_id"] == failed["desktop_id"]
    assert recovered["channel_attempt_id"] != failed["channel_attempt_id"]


async def test_real_pending_claim_loses_to_a_committed_revoke(target, cloud_boundary):
    original = await claimed(target)
    await channel.wuying_channel.revoke(original)
    with pytest.raises(channel.ChannelVerificationStopped):
        await channel.wuying_channel.begin(original)
    current = await cloud_desktop_repo.get(original["id"])
    assert current["channel_enrollment_grant"] is None
    with pytest.raises(channel.ChannelVerificationStopped):
        await channel.wuying_channel.begin(current)


@pytest.mark.parametrize("flow", ["create", "start", "channel"])
async def test_queued_snapshot_cannot_claim_a_successor_attempt(target, lifecycle_cloud, flow):
    from sandbox.wuying_desktop_service import WuyingDesktopService
    service = WuyingDesktopService()
    expected = dict(target.record)
    args = (target.workspace, target.record["id"])
    if flow == "create":
        queued = service._create_flow(*args, None, expected=expected)
    elif flow == "start":
        queued = service._start_flow(*args, target.record["desktop_id"], expected=expected)
    else:
        queued = service._channel_flow(*args, expected=expected)
    successor = await channel.wuying_channel.begin(target.record)
    before = list(lifecycle_cloud)
    await queued
    assert lifecycle_cloud == before
    assert (await cloud_desktop_repo.get(target.record["id"]))["channel_attempt_id"] == successor.record["channel_attempt_id"]


async def test_provision_queue_keeps_its_original_snapshot(target, lifecycle_cloud, monkeypatch):
    from sandbox import entitlement
    from sandbox.wuying_desktop_service import WuyingDesktopService
    monkeypatch.setattr(entitlement, "subscription_sandbox_enabled", lambda: False)
    await cloud_desktop_repo.update(target.record["id"], status="stopped")
    service, queued = WuyingDesktopService(), []
    monkeypatch.setattr(service, "_spawn", lambda _workspace, coro: queued.append(coro))
    try:
        assert (await service.provision(target.workspace))["state"] == "starting"
        assert len(queued) == 1
        fresh = await cloud_desktop_repo.get(target.record["id"])
        successor = await channel.wuying_channel.begin(fresh)
        before = list(lifecycle_cloud)
        await queued.pop()
        assert lifecycle_cloud == before
        assert (await cloud_desktop_repo.get(fresh["id"]))["channel_attempt_id"] == successor.record["channel_attempt_id"]
    finally:
        for coro in queued:
            coro.close()


async def test_initial_claim_cas_serializes_independent_transactions(target, monkeypatch, record_property):
    import json
    from db.repository import cloud_desktop_repo as repository
    if get_engine().dialect.name != "postgresql":
        results = await asyncio.gather(*(channel.wuying_channel.begin(target.record) for _ in range(2)),
                                       return_exceptions=True)
        assert sum(isinstance(item, channel.ChannelAttempt) for item in results) == 1
        assert sum(isinstance(item, channel.ChannelVerificationStopped) for item in results) == 1
        return
    original = repository.get_db_session
    locked, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pids = {}

    @asynccontextmanager
    async def held_transaction():
        async with original() as db:
            name = asyncio.current_task().get_name()
            if name in ("claim-winner", "claim-loser"):
                pids[name] = await db.scalar(text("SELECT pg_backend_pid()"))
                if name == "claim-loser":
                    entered.set()
            yield db
            if name == "claim-winner":
                locked.set()
                await release.wait()

    monkeypatch.setattr(repository, "get_db_session", held_transaction)
    winner = asyncio.create_task(channel.wuying_channel.begin(target.record), name="claim-winner")
    loser = None
    try:
        await asyncio.wait_for(locked.wait(), 3)
        loser = asyncio.create_task(channel.wuying_channel.begin(target.record), name="claim-loser")
        await asyncio.wait_for(entered.wait(), 3)
        async with asyncio.timeout(3):
            while True:
                assert not loser.done(), "Claim bypassed the held SQL update"
                async with original() as db:
                    witness = (await db.execute(text(
                        "SELECT pid, wait_event_type, pg_blocking_pids(pid) AS blockers "
                        "FROM pg_stat_activity WHERE pid=:pid"), {"pid": pids["claim-loser"]})).mappings().one()
                if witness["wait_event_type"] == "Lock" and pids["claim-winner"] in witness["blockers"]:
                    break
                await asyncio.sleep(.01)
        record_property("claim_lock_witness", json.dumps({"pids": pids, "wait": dict(witness)}))
        release.set()
        won = await asyncio.wait_for(winner, 3)
        with pytest.raises(channel.ChannelVerificationStopped):
            await asyncio.wait_for(loser, 3)
        assert (await cloud_desktop_repo.get(target.record["id"]))["channel_attempt_id"] == won.record["channel_attempt_id"]
    finally:
        release.set()
        await asyncio.gather(*(task for task in (winner, loser) if task), return_exceptions=True)


async def test_late_port_reservation_cannot_modify_revoked_attempt(target, cloud_boundary, monkeypatch):
    port = target.record["tunnel_port"]
    await cloud_desktop_repo.update(target.record["id"], tunnel_port=None)
    target.record = await cloud_desktop_repo.get(target.record["id"])
    target.config.wuying_tunnel_port_range = f"{port}-{port}"
    waiting, release = asyncio.Event(), asyncio.Event()
    reserve = cloud_desktop_repo.reserve_attempt_port

    async def delayed(expected, low, high):
        waiting.set()
        await release.wait()
        return await reserve(expected, low, high)

    monkeypatch.setattr(cloud_desktop_repo, "reserve_attempt_port", delayed)
    task = asyncio.create_task(channel.wuying_channel.install(target.record))
    try:
        await asyncio.wait_for(waiting.wait(), 4)
        await channel.wuying_channel.revoke(target.record)
        release.set()
        with pytest.raises(channel.ChannelVerificationStopped):
            await asyncio.wait_for(task, 4)
        current = await cloud_desktop_repo.get(target.record["id"])
        assert current["tunnel_port"] is None and current["tunnel_state"] == "revoked"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_late_revoke_response_cannot_clear_new_claims_port(target, cloud_boundary, monkeypatch):
    waiting, release = asyncio.Event(), asyncio.Event()
    command = channel.run_desktop_command

    async def delayed(desktop_id, script, timeout=300):
        if script == "systemctl disable --now openbox-tunnel":
            waiting.set()
            await release.wait()
            return "fixture stop response"
        return await command(desktop_id, script, timeout=timeout)

    monkeypatch.setattr(channel, "run_desktop_command", delayed)
    task = asyncio.create_task(channel.wuying_channel.revoke(target.record))
    try:
        await asyncio.wait_for(waiting.wait(), 4)
        await claimed(target)
        successor = await channel.wuying_channel.begin(target.record)
        release.set()
        with pytest.raises(channel.ChannelVerificationStopped):
            await asyncio.wait_for(task, 4)
        current = await cloud_desktop_repo.get(target.record["id"])
        assert current["tunnel_port"] == successor.record["tunnel_port"]
        assert current["tunnel_bind"] == successor.record["tunnel_bind"]
        assert current["channel_attempt_id"] == successor.record["channel_attempt_id"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_process_interruption_keeps_retryable_durable_attempt(target, remote, cloud_boundary, monkeypatch):
    from sandbox.wuying_desktop_service import WuyingDesktopService
    waiting, release = asyncio.Event(), asyncio.Event()
    command = channel.run_desktop_command

    async def interrupted(desktop_id, script, timeout=300):
        if "OPENBOX_PUBKEY=" in script:
            waiting.set()
            await release.wait()
        return await command(desktop_id, script, timeout=timeout)

    monkeypatch.setattr(channel, "run_desktop_command", interrupted)
    task = asyncio.create_task(WuyingDesktopService()._create_flow(target.workspace, target.record["id"], None))
    try:
        await asyncio.wait_for(waiting.wait(), 4)
        pending = await cloud_desktop_repo.get(target.record["id"])
        assert pending["tunnel_state"] == "pending" and pending["channel_attempt_id"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(channel, "run_desktop_command", command)
        await WuyingDesktopService()._channel_flow(target.workspace, pending["id"])
        current = await cloud_desktop_repo.get(pending["id"])
        assert current["status"] == "running" and current["tunnel_state"] == "up"
        assert current["channel_attempt_id"] != pending["channel_attempt_id"]
        assert current["desktop_id"] == pending["desktop_id"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_actual_pool_install_failure_compensates_then_new_claim_recovers(
    target, remote, lifecycle_cloud, monkeypatch,
):
    from sandbox.pool import PoolService
    await claimed(target)
    command = channel.run_desktop_command

    async def fail(desktop_id, script, timeout=300):
        if "OPENBOX_PUBKEY=" in script:
            raise RuntimeError("fixture partial install failed")
        return await command(desktop_id, script, timeout=timeout)

    monkeypatch.setattr(channel, "run_desktop_command", fail)
    with pytest.raises(RuntimeError, match="partial install failed"):
        await PoolService().assign_claimed(target.record, target.workspace, target.owner)
    failed = await cloud_desktop_repo.get(target.record["id"])
    assert failed["pool_state"] == "prewarm" and failed["workspace_id"] is None
    assert failed["tunnel_state"] == "revoked"
    target.record = await cloud_desktop_repo.claim_prewarm(target.workspace, target.owner,
        usable_until=datetime.now(timezone.utc) + timedelta(days=365))
    assert target.record["id"] == failed["id"]
    monkeypatch.setattr(channel, "run_desktop_command", command)
    result = await PoolService().assign_claimed(target.record, target.workspace, target.owner)
    assert result["pool_state"] == "assigned" and result["tunnel_state"] == "up"
    assert result["channel_attempt_id"] != failed["channel_attempt_id"]
    assert "create" not in lifecycle_cloud


async def test_pool_compensation_stops_after_inflight_clear_loses_assignment(
    target, lifecycle_cloud, monkeypatch,
):
    from sandbox import wuying_ecd
    from sandbox.pool import PoolService
    await claimed(target)
    command = channel.run_desktop_command
    entitlement = wuying_ecd.modify_entitlement
    waiting, release = asyncio.Event(), asyncio.Event()

    async def fail(desktop_id, script, timeout=300):
        if "OPENBOX_PUBKEY=" in script:
            raise RuntimeError("fixture partial install failed")
        return await command(desktop_id, script, timeout=timeout)

    async def delayed(desktop_id, users):
        await entitlement(desktop_id, users)
        if not users:
            waiting.set()
            await release.wait()

    monkeypatch.setattr(channel, "run_desktop_command", fail)
    monkeypatch.setattr(wuying_ecd, "modify_entitlement", delayed)
    task = asyncio.create_task(PoolService().assign_claimed(target.record, target.workspace, target.owner))
    try:
        await asyncio.wait_for(waiting.wait(), 4)
        await cloud_desktop_repo.update(target.record["id"], pool_state="released", workspace_id=None,
            assigned_at=None, channel_error="successor")
        before = list(lifecycle_cloud)
        release.set()
        with pytest.raises(channel.ChannelVerificationStopped):
            await asyncio.wait_for(task, 4)
        assert lifecycle_cloud == before
        current = await cloud_desktop_repo.get(target.record["id"])
        assert current["pool_state"] == "released" and current["channel_error"] == "successor"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_activation_final_desktop_and_job_changes_rollback_together(
    target, remote, lifecycle_cloud, monkeypatch,
):
    from sandbox.desktop_activation import DesktopActivationService
    await claimed(target)
    await cloud_desktop_repo.update(target.record["id"], status="starting")
    await activation_job(target)
    write = cloud_desktop_repo.write_channel_attempt
    updated = []

    async def lose_job_before_commit(expected, fields, **kwargs):
        if fields.get("status") == "running":
            async with get_db_session() as db:
                job = await db.get(DesktopActivation, target.workspace)
                job.lease_owner = "successor-activation"
                job.lease_until = datetime.now(timezone.utc) + timedelta(minutes=1)
            result = await write(expected, fields, **kwargs)
            updated.append(result["pool_state"])
            return result
        return await write(expected, fields, **kwargs)

    monkeypatch.setattr(cloud_desktop_repo, "write_channel_attempt", lose_job_before_commit)
    assert await DesktopActivationService().process(target.workspace) is False
    assert updated == ["assigned"]  # The desktop UPDATE ran, then the job CAS failed.
    current = await cloud_desktop_repo.get(target.record["id"])
    assert current["pool_state"] == "assigning" and current["status"] == "starting"
    async with get_db_session() as db:
        job = await db.get(DesktopActivation, target.workspace)
        assert job.lease_owner == "successor-activation" and job.state != "ready"


@pytest.mark.parametrize("pool_enabled", [False, True])
@pytest.mark.parametrize("loss", ["lease", "subscription"])
async def test_initial_inventory_cannot_allocate_after_paid_job_loses_authority(
    target, lifecycle_cloud, monkeypatch, pool_enabled, loss,
):
    from sandbox import wuying_ecd
    from sandbox.desktop_activation import DesktopActivationService
    target.config.pool_enabled = pool_enabled
    target.config.pool_assign_on_provision = pool_enabled
    await cloud_desktop_repo.update(target.record["id"], workspace_id=None, user_id=None,
        pool_state="prewarm" if pool_enabled else "released", tunnel_state="revoked",
        charge_type="PrePaid", expires_at=datetime.now(timezone.utc) + timedelta(days=3650))
    await activation_job(target)
    waiting, release = asyncio.Event(), asyncio.Event()

    async def inventory(**_kwargs):
        waiting.set()
        await release.wait()
        return []

    monkeypatch.setattr(wuying_ecd, "list_desktops", inventory)
    task = asyncio.create_task(DesktopActivationService().process(target.workspace))
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        async with get_db_session() as db:
            if loss == "lease":
                job = await db.get(DesktopActivation, target.workspace)
                job.lease_owner = "replacement-worker"
            else:
                await db.execute(update(BillingSubscription).where(
                    BillingSubscription.workspace_id == target.workspace).values(
                    ends_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        before = list(lifecycle_cloud)
        release.set()
        assert await asyncio.wait_for(task, 4) is False
        assert lifecycle_cloud == before
        assert await cloud_desktop_repo.get_for_workspace(target.workspace) is None
        original = await cloud_desktop_repo.get(target.record["id"])
        assert original["workspace_id"] is None and original["channel_enrollment_grant"] is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("pause", ["end_user", "throttle"])
async def test_real_create_desktop_rechecks_before_each_purchase_submission(
    target, lifecycle_cloud, monkeypatch, pause,
):
    from sandbox import wuying_ecd
    from sandbox.wuying_desktop_service import WuyingDesktopService
    for key, value in {"wuying_image_id": "image-fixture", "wuying_office_site_id": "cn-fixture+directory",
                       "wuying_policy_group_id": "policy-fixture", "wuying_charge_type": "PostPaid"}.items():
        setattr(target.config, key, value)
    monkeypatch.setattr(wuying_ecd, "get_config", lambda: target.config)
    await cloud_desktop_repo.update(target.record["id"], desktop_id=None, status="creating", tunnel_state="pending")
    target.record = await cloud_desktop_repo.get(target.record["id"])
    waiting, release = asyncio.Event(), asyncio.Event()
    submitted = []

    async def end_user(*_args):
        if pause == "end_user":
            waiting.set()
            await release.wait()
        return "fixture-end-user", False

    class Client:
        async def create_desktops_async(self, _request):
            submitted.append("CreateDesktops")
            raise RuntimeError("Throttling.User: fixture flow control")

    async def backoff(_seconds):
        waiting.set()
        await release.wait()

    monkeypatch.setattr(wuying_ecd, "create_desktop", actual_create_desktop)
    monkeypatch.setattr(wuying_ecd, "ensure_end_user", end_user)
    monkeypatch.setattr(wuying_ecd, "ecd_client", lambda: Client())
    # Replace this module's backoff boundary, not global asyncio.sleep or the
    # production retry helper. No SDK request leaves the in-process client.
    monkeypatch.setattr(wuying_ecd, "asyncio", SimpleNamespace(sleep=backoff))
    task = asyncio.create_task(WuyingDesktopService()._create_flow(
        target.workspace, target.record["id"], None, expected=dict(target.record)))
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        await channel.wuying_channel.revoke(target.record)
        release.set()
        await asyncio.wait_for(task, 4)
        assert len(submitted) == (0 if pause == "end_user" else 1)
        current = await cloud_desktop_repo.get(target.record["id"])
        assert current["desktop_id"] is None and current["tunnel_state"] == "revoked"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
