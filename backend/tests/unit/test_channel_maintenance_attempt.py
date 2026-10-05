"""Maintenance uses production SQL; every cloud effect is an explicit local stub."""
import asyncio
from contextlib import asynccontextmanager
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, update

from core.config import OpenBoxConfig
from db.base import get_db_session, get_engine
from db.models.cloud_desktop import CloudDesktop
from db.models.desktop_activation import DesktopActivation
from db.models.billing import BillingSubscription, PaymentOrder
from db.repository.cloud_desktop_repo import cloud_desktop_repo as desktops
from sandbox import channel, desktop_activation, entitlement, pool, wuying_ecd as ecd
from sandbox.browser_runtime import RUNTIME_VERSION
from sandbox.wuying_desktop_service import WuyingDesktopService
from sandbox.wuying_ecd import (
    modify_entitlement as actual_entitlement, rebuild_desktop as actual_rebuild,
    tag_desktop as actual_tag, renew_desktop as actual_renew,
    delete_desktop as actual_delete, wait_desktop_ready as actual_wait,
)
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


@pytest.fixture
async def maintenance(monkeypatch):
    owner, _, workspace = await accounts()
    stamp = datetime.now(timezone.utc)
    config = OpenBoxConfig(sandbox_provider="wuying", wuying_routing="per_desktop",
        wuying_region_id="cn-maintenance-fixture", wuying_image_id="image-fixture",
        wuying_policy_group_id="policy-fixture", wuying_channel_key="11" * 32)
    for module in (channel, desktop_activation, entitlement, pool, ecd):
        monkeypatch.setattr(module, "get_config", lambda: config)
    monkeypatch.setattr("core.config.get_config", lambda: config)
    record = await desktops.create(workspace, config.wuying_region_id, "running",
        user_id=owner, desktop_id="maintenance-" + workspace, pool_state="assigned",
        end_user_id="fixture-end-user", assigned_at=stamp, charge_type="PrePaid",
        expires_at=stamp + timedelta(days=30), tunnel_state="up", channel_kind="ssh")
    config.pool_adopt_allowlist = record["desktop_id"]
    data = SimpleNamespace(owner=owner, workspace=workspace, record=record, config=config,
        calls=[], pause=None, reached=asyncio.Event(), release=asyncio.Event(), fail=None)
    data.release.set()

    async def step(name):
        data.calls.append(name)
        if name == data.pause:
            data.reached.set()
            await data.release.wait()
        if name == data.fail:
            raise RuntimeError("fixture " + name + " failure")

    async def describe(*_args, **_kwargs):
        await step("describe")
        return dict(desktop_id=record["desktop_id"], status="Running", image_id="image-fixture",
            policy_group_id="policy-fixture", charge_type="PrePaid", end_user_ids=["fixture-end-user"],
            expired_time=(stamp + timedelta(days=60)).isoformat())

    async def ownership(*_args):
        await step("ownership")
        return "fixture-end-user"

    async def command(_desktop, script, **_kwargs):
        name = "stop" if "systemctl stop" in script else "runtime" if "repair_browser_runtime" in script else "command"
        await step(name)
        return json.dumps(dict(version=RUNTIME_VERSION, ready=True)) if name == "runtime" else "fixture-host\n"

    async def inventory(**_kwargs):
        await step("inventory")
        return [await describe()]

    async def tags(*_args):
        await step("tags")
        return {}

    def boundary(name):
        async def invoke(*_args, **_kwargs):
            if name == "wait_desktop_ready" and _kwargs.get("on_rebuild_observed"):
                # Explicit stand-in for the remote transition. Separate cases
                # below drive the real wait loop and local SDK transport.
                await _kwargs["on_rebuild_observed"]()
            await step(name)
            if name == "delete_desktop":
                return []
            return {"order_id": "fixture-order"}
        return invoke

    def forbidden(*_args, **_kwargs):
        raise AssertionError("real cloud client is forbidden")

    monkeypatch.setattr(ecd, "ecd_client", forbidden)
    monkeypatch.setattr(ecd, "eds_user_client", forbidden)
    monkeypatch.setattr(ecd, "describe_desktop", describe)
    monkeypatch.setattr(ecd, "verify_ownership", ownership)
    monkeypatch.setattr(ecd, "list_desktops", inventory)
    monkeypatch.setattr(ecd, "desktop_tags", tags)
    for name in ("modify_entitlement", "disconnect_desktop_sessions", "untag_desktop",
                 "tag_desktop", "rebuild_desktop", "wait_desktop_ready", "modify_policy_group",
                 "renew_desktop", "delete_desktop"):
        monkeypatch.setattr(ecd, name, boundary(name))
    monkeypatch.setattr(channel, "run_desktop_command", command)
    monkeypatch.setattr(pool, "run_desktop_command", command)
    data.step = step
    try:
        yield data
    finally:
        data.release.set()
        # PostgreSQL acceptance retains earlier cases' rows. Do not let a
        # later pool claim select this case's completed synthetic capacity.
        await desktops.update(data.record["id"], pool_state="retired", workspace_id=None,
            user_id=None, channel_enrollment_grant=None)


async def prepare(maintenance, flow):
    if flow in ("recycle", "retire", "adopt", "renew"):
        await desktops.update(maintenance.record["id"], workspace_id=None, user_id=None,
            assigned_at=None, pool_state="prewarm", tunnel_state="revoked")
    if flow == "free":
        stamp = datetime.now(timezone.utc)
        async with get_db_session() as db:
            db.add(DesktopActivation(workspace_id=maintenance.workspace, user_id=maintenance.owner,
                state="ready", step="ready", attempts=0, next_run_at=stamp, created_at=stamp, updated_at=stamp))
    if flow == "ghost":
        await desktops.update(maintenance.record["id"], charge_type="PostPaid")
    maintenance.record = await desktops.get(maintenance.record["id"])


async def run(maintenance, flow):
    did = maintenance.record["desktop_id"]
    service = pool.PoolService()
    if flow == "free":
        return await desktop_activation.DesktopActivationService().process(maintenance.workspace)
    if flow == "ghost":
        return await WuyingDesktopService().release_ghost(maintenance.workspace, maintenance.owner)
    if flow == "recycle":
        return await service.recycle(did, maintenance.owner, approve=True)
    if flow == "adopt":
        return await service.adopt(did, "prewarm", maintenance.owner)
    if flow == "renew":
        return await service.renew(did, maintenance.owner, approve=True)
    return await getattr(service, flow)(did, maintenance.owner)


async def replace(maintenance):
    # Independent transaction, retaining the same physical desktop and row:
    # an old cleanup cannot claim the replacement merely by matching its id.
    await desktops.update(maintenance.record["id"], workspace_id=maintenance.workspace,
        user_id=maintenance.owner, pool_state="assigned", tunnel_state="up",
        assigned_at=datetime.now(timezone.utc) + timedelta(days=1),
        channel_attempt_id="successor-" + maintenance.workspace, channel_error="successor")
    return await desktops.get(maintenance.record["id"])


@pytest.mark.parametrize("flow,pause", [
    ("free", "ownership"), ("release", "modify_entitlement"),
    ("recycle", "rebuild_desktop"), ("ghost", "delete_desktop"),
    ("retire", "tag_desktop"), ("adopt", "tags"), ("renew", "renew_desktop"),
])
async def test_old_maintenance_cannot_touch_replacement_after_cloud_io(maintenance, flow, pause):
    await prepare(maintenance, flow)
    maintenance.pause = pause
    maintenance.release.clear()
    task = asyncio.create_task(run(maintenance, flow))
    try:
        await asyncio.wait_for(maintenance.reached.wait(), 5)
        assert get_engine().sync_engine.pool.checkedout() == 0
        successor = await replace(maintenance)
        before = len(maintenance.calls)
        maintenance.release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        current = await desktops.get(maintenance.record["id"])
        assert current == successor
        assert maintenance.calls[before:] == []
    finally:
        maintenance.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("flow", ["free", "release", "recycle", "ghost", "retire", "adopt", "renew"])
async def test_normal_maintenance_completes_without_opening_revoked_channels(maintenance, flow):
    await prepare(maintenance, flow)
    result = await run(maintenance, flow)
    current = await desktops.get(maintenance.record["id"])
    if flow == "ghost":
        assert current is None and maintenance.calls.count("delete_desktop") == 1
        return
    assert current["desktop_id"] == maintenance.record["desktop_id"]
    assert current["channel_enrollment_grant"] is None
    if flow == "free":
        assert result is True
        assert current["workspace_id"] == maintenance.workspace and current["tunnel_state"] == "up"
        async with get_db_session() as db:
            assert (await db.get(DesktopActivation, maintenance.workspace)).step == "suspended"
        assert maintenance.calls[-2:] == ["modify_entitlement", "disconnect_desktop_sessions"]
    elif flow == "renew":
        assert current["expires_at"] > maintenance.record["expires_at"]
        assert current["tunnel_state"] == "revoked"
    else:
        assert current["pool_state"] == {"release": "released", "recycle": "prewarm", "retire": "retired", "adopt": "prewarm"}[flow]
        assert current["tunnel_state"] == "revoked" and current["workspace_id"] is None
        if flow in ("recycle", "adopt"):
            assert current["channel_kind"] is None


@pytest.mark.parametrize("flow", ["free", "release", "recycle", "ghost", "retire", "adopt", "renew"])
async def test_final_cas_cannot_overwrite_replacement(maintenance, monkeypatch, flow):
    await prepare(maintenance, flow)
    waiting, release = asyncio.Event(), asyncio.Event()
    original = desktops.write_channel_attempt

    async def paused(expected, fields, **kwargs):
        final = {"release": fields.get("pool_state") == "released",
            "recycle": fields.get("pool_state") == "prewarm",
            "adopt": fields.get("pool_state") == "prewarm",
            "retire": fields.get("pool_state") == "retired",
            "ghost": fields.get("is_deleted") is True,
            "renew": "expires_at" in fields,
            "free": "disconnect_desktop_sessions" in maintenance.calls}[flow]
        if final:
            waiting.set()
            await release.wait()
        return await original(expected, fields, **kwargs)

    monkeypatch.setattr(desktops, "write_channel_attempt", paused)
    if flow == "free" and get_engine().dialect.name == "sqlite":
        # SQLite has one database writer. Its real balance lock already owns
        # that slot; arrange the competing write before that transaction begins.
        original_lock = desktop_activation.lock_balance

        async def before_lock(*args, **kwargs):
            if "disconnect_desktop_sessions" in maintenance.calls:
                waiting.set()
                await release.wait()
            return await original_lock(*args, **kwargs)

        monkeypatch.setattr(desktop_activation, "lock_balance", before_lock)
    task = asyncio.create_task(run(maintenance, flow))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        successor = await replace(maintenance)
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        assert await desktops.get(maintenance.record["id"]) == successor
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def paid(maintenance):
    stamp = datetime.now(timezone.utc)
    oid = "maintenance-order-" + maintenance.workspace
    async with get_db_session() as db:
        db.add(PaymentOrder(id=oid, workspace_id=maintenance.workspace, user_id=maintenance.owner,
            request_key=oid, provider="fixture", amount_fen=100, credits=1,
            currency="CNY", kind="subscription", status="paid", created_at=stamp))
        await db.flush()
        db.add(BillingSubscription(order_id=oid, workspace_id=maintenance.workspace, plan_id="pro",
            cycle="monthly", plan={}, starts_at=stamp - timedelta(days=1), ends_at=stamp + timedelta(days=1)))


@pytest.mark.parametrize("change", ["paid", "lease"])
async def test_free_cleanup_stops_when_original_job_authority_is_lost(maintenance, change):
    await prepare(maintenance, "free")
    maintenance.pause = "ownership"
    maintenance.release.clear()
    task = asyncio.create_task(run(maintenance, "free"))
    try:
        await asyncio.wait_for(maintenance.reached.wait(), 4)
        if change == "paid":
            await paid(maintenance)
        else:
            async with get_db_session() as db:
                await db.execute(update(DesktopActivation).where(
                    DesktopActivation.workspace_id == maintenance.workspace).values(lease_owner="new-owner"))
        maintenance.release.set()
        assert await asyncio.wait_for(task, 4) is False
        assert "modify_entitlement" not in maintenance.calls
        assert "disconnect_desktop_sessions" not in maintenance.calls
        async with get_db_session() as db:
            assert (await db.get(DesktopActivation, maintenance.workspace)).step != "suspended"
    finally:
        maintenance.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("flow,failed", [("release", "untag_desktop"), ("recycle", "modify_entitlement"), ("adopt", "modify_entitlement")])
async def test_ordinary_failure_retries_complete_same_original_desktop(maintenance, flow, failed):
    await prepare(maintenance, flow)
    maintenance.fail = failed
    with pytest.raises(RuntimeError):
        await run(maintenance, flow)
    incomplete = await desktops.get(maintenance.record["id"])
    assert incomplete["tunnel_state"] == "revoked"
    maintenance.fail = None
    await run(maintenance, flow)
    completed = await desktops.get(maintenance.record["id"])
    assert completed["pool_state"] == ("released" if flow == "release" else "prewarm")
    if flow == "recycle":
        assert maintenance.calls.count("rebuild_desktop") == 1


async def test_interrupted_rebuild_reconciles_without_resubmission(maintenance):
    await prepare(maintenance, "recycle")
    maintenance.pause = "wait_desktop_ready"
    maintenance.release.clear()
    task = asyncio.create_task(run(maintenance, "recycle"))
    try:
        await asyncio.wait_for(maintenance.reached.wait(), 4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        original = await desktops.get(maintenance.record["id"])
        assert original["status"] == "rebuild_observed" and original["pool_state"] == "recycling"
        maintenance.pause = None
        maintenance.release.set()
        await run(maintenance, "recycle")  # New service, durable SQL state only.
        assert maintenance.calls.count("rebuild_desktop") == 1
        assert (await desktops.get(original["id"]))["pool_state"] == "prewarm"
    finally:
        maintenance.release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_interruption_before_rebuild_cannot_reuse_old_running_state(maintenance):
    await prepare(maintenance, "recycle")
    await desktops.update(maintenance.record["id"], golden_image_id=maintenance.config.wuying_image_id)
    maintenance.pause = "command"  # Real _stop_revoked boundary, before _rebuild.
    maintenance.release.clear()
    task = asyncio.create_task(run(maintenance, "recycle"))
    try:
        await asyncio.wait_for(maintenance.reached.wait(), 4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        original = await desktops.get(maintenance.record["id"])
        assert original["status"] == "running" and original["pool_state"] == "recycling"
        assert "rebuild_desktop" not in maintenance.calls
        maintenance.pause = None
        maintenance.release.set()
        await run(maintenance, "recycle")
        assert maintenance.calls.count("rebuild_desktop") == 1
        assert (await desktops.get(original["id"]))["pool_state"] == "prewarm"
    finally:
        maintenance.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("flow,operation,actual,sdk", [
    ("release", "modify_entitlement", actual_entitlement, "modify_entitlement_async"),
    ("recycle", "rebuild_desktop", actual_rebuild, "rebuild_desktops_async"),
    ("retire", "tag_desktop", actual_tag, "tag_resources_async"),
    ("renew", "renew_desktop", actual_renew, "renew_desktops_async"),
])
@pytest.mark.parametrize("pause", ["before_submit", "retry", "response"])
async def test_actual_sdk_boundary_rejects_late_work(maintenance, monkeypatch, flow, operation, actual, sdk, pause):
    await prepare(maintenance, flow)
    original_retry = ecd._retry_throttled
    calls = []
    waiting, release = asyncio.Event(), asyncio.Event()

    async def submit(_request):
        calls.append(sdk)
        if pause == "retry":
            raise RuntimeError("Throttling fixture")
        if pause == "response":
            waiting.set()
            await release.wait()
        return SimpleNamespace(body=SimpleNamespace(request_id="fixture"))

    async def delayed_retry(call, *args, **kwargs):
        if pause == "before_submit":
            waiting.set()
            await release.wait()
        return await original_retry(call, *args, **kwargs)

    async def backoff(_seconds):
        waiting.set()
        await release.wait()

    monkeypatch.setattr(ecd, operation, actual)
    monkeypatch.setattr(ecd, "ecd_client", lambda: SimpleNamespace(**{sdk: submit}))
    monkeypatch.setattr(ecd, "_retry_throttled", delayed_retry)
    if pause == "retry":
        # Replace only this module's backoff binding, never global asyncio.
        monkeypatch.setattr(ecd, "asyncio", SimpleNamespace(sleep=backoff))
    task = asyncio.create_task(run(maintenance, flow))
    try:
        await asyncio.wait_for(waiting.wait(), 4)
        successor = await replace(maintenance)
        before = len(maintenance.calls)
        release.set()
        outcome = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 4))[0]
        assert isinstance(outcome, channel.ChannelVerificationStopped)
        assert calls == ([] if pause == "before_submit" else [sdk])
        assert maintenance.calls[before:] == []
        assert await desktops.get(maintenance.record["id"]) == successor
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_absent_adoption_serializes_actual_database_transactions(maintenance, monkeypatch, record_property):
    from db.repository import cloud_desktop_repo as repository
    did = "absent-" + maintenance.workspace
    args = (None, did, maintenance.config.wuying_region_id)
    original = repository.get_db_session
    if get_engine().dialect.name != "postgresql":
        results = await asyncio.gather(*(desktops.reserve_adoption(*args) for _ in range(2)))
        assert sum(isinstance(item, dict) for item in results) == 1
        assert results.count(None) == 1
        return
    locked, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pids = {}

    @asynccontextmanager
    async def held():
        async with original() as db:
            name = asyncio.current_task().get_name()
            if name in ("adopt-winner", "adopt-loser"):
                pids[name] = await db.scalar(text("SELECT pg_backend_pid()"))
                if name == "adopt-loser":
                    entered.set()
            yield db
            if name == "adopt-winner":
                locked.set()
                await release.wait()

    monkeypatch.setattr(repository, "get_db_session", held)
    winner = asyncio.create_task(desktops.reserve_adoption(*args), name="adopt-winner")
    loser = None
    try:
        await asyncio.wait_for(locked.wait(), 4)
        loser = asyncio.create_task(desktops.reserve_adoption(*args), name="adopt-loser")
        await asyncio.wait_for(entered.wait(), 4)
        async with asyncio.timeout(4):
            while True:
                assert not loser.done(), "Second adoption bypassed held SQL reservation"
                async with original() as db:
                    witness = (await db.execute(text("SELECT pid, wait_event_type, pg_blocking_pids(pid) AS blockers "
                        "FROM pg_stat_activity WHERE pid=:pid"), {"pid": pids["adopt-loser"]})).mappings().one()
                if witness["wait_event_type"] == "Lock" and pids["adopt-winner"] in witness["blockers"]:
                    break
                await asyncio.sleep(.01)
        record_property("adoption_lock_witness", json.dumps({"pids": pids, "wait": dict(witness)}))
        release.set()
        assert (await winner)["desktop_id"] == did
        assert await loser is None
    finally:
        release.set()
        await asyncio.gather(*(t for t in (winner, loser) if t), return_exceptions=True)


async def test_maintenance_never_confers_install_authority(maintenance):
    await prepare(maintenance, "recycle")
    original = maintenance.record
    attempt = await channel.wuying_channel.maintain(original, revoke=True)
    assert attempt.record["tunnel_state"] == "revoked" and attempt.record["channel_enrollment_grant"] is None
    with pytest.raises(ValueError, match="cannot open"):
        await attempt.write(tunnel_state="pending")
    with pytest.raises(channel.ChannelVerificationStopped, match="does not authorize"):
        await attempt.install()
    with pytest.raises(channel.ChannelVerificationStopped, match="does not authorize"):
        await channel.wuying_channel.install(original, attempt=attempt)
    with pytest.raises(channel.ChannelVerificationStopped):
        await channel.wuying_channel.begin(attempt.record)
    assert maintenance.calls == []
    assert (await desktops.get(original["id"]))["tunnel_state"] == "revoked"


async def test_live_renewal_preserves_existing_attempt_and_channel(maintenance):
    original = await channel.wuying_channel.begin(maintenance.record)
    renewed = await pool.PoolService().renew(original.record["desktop_id"], maintenance.owner, approve=True)
    assert renewed["channel_attempt_id"] == original.record["channel_attempt_id"]
    assert renewed["tunnel_state"] == "up"
    assert renewed["workspace_id"] == maintenance.workspace
    assert renewed["expires_at"] > original.record["expires_at"]


async def test_queued_pool_renewal_keeps_original_assignment(maintenance, monkeypatch):
    await prepare(maintenance, "renew")
    maintenance.config.pool_enabled = True
    maintenance.config.pool_auto_renew = True
    await desktops.update(maintenance.record["id"], expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    service = pool.PoolService()
    original = service.renew
    successor = None

    async def queued(desktop_id, *args, expected=None, **kwargs):
        nonlocal successor
        assert desktop_id == maintenance.record["desktop_id"]
        assert expected["pool_state"] == "prewarm"
        successor = await replace(maintenance)
        return await original(desktop_id, *args, expected=expected, **kwargs)

    monkeypatch.setattr(service, "renew", queued)
    with pytest.raises(channel.ChannelVerificationStopped):
        await service.renew_expiring(actor=maintenance.owner)
    assert successor is not None
    assert maintenance.calls == []
    assert await desktops.get(successor["id"]) == successor


async def test_definite_rebuild_rejection_allows_explicit_retry(maintenance, monkeypatch):
    await prepare(maintenance, "recycle")
    submissions = []

    async def submit(request):
        submissions.append(list(request.desktop_id))
        result = {"DesktopId": maintenance.record["desktop_id"],
                  "Code": "OperationDenied" if len(submissions) == 1 else "Success"}
        return SimpleNamespace(body=SimpleNamespace(request_id="fixture", rebuild_results=[
            SimpleNamespace(to_map=lambda: result)]))

    monkeypatch.setattr(ecd, "rebuild_desktop", actual_rebuild)
    monkeypatch.setattr(ecd, "ecd_client", lambda: SimpleNamespace(rebuild_desktops_async=submit))
    with pytest.raises(ecd.RebuildRejected):
        await run(maintenance, "recycle")
    rejected = await desktops.get(maintenance.record["id"])
    assert rejected["status"] == "rebuild_failed" and rejected["pool_state"] == "recycling"
    await run(maintenance, "recycle")
    assert len(submissions) == 2
    assert (await desktops.get(rejected["id"]))["pool_state"] == "prewarm"


@pytest.mark.parametrize("retry_entry", ["recycle", "adopt"])
async def test_unknown_rebuild_cannot_reuse_old_running_image(maintenance, monkeypatch, retry_entry):
    await prepare(maintenance, "recycle")
    submissions = []

    async def submit(request):
        submissions.append(list(request.desktop_id))
        raise ConnectionError("fixture lost response; acceptance unknown")

    monkeypatch.setattr(ecd, "rebuild_desktop", actual_rebuild)
    monkeypatch.setattr(ecd, "ecd_client", lambda: SimpleNamespace(rebuild_desktops_async=submit))
    with pytest.raises(ConnectionError):
        await run(maintenance, "recycle")
    uncertain = await desktops.get(maintenance.record["id"])
    assert uncertain["status"] == "rebuild_pending" and uncertain["pool_state"] == "recycling"
    with pytest.raises(pool.PoolStateError, match="unconfirmed"):
        await run(maintenance, retry_entry)
    current = await desktops.get(uncertain["id"])
    assert current["status"] == "rebuild_pending" and current["pool_state"] == "recycling"
    assert len(submissions) == 1
    assert "wait_desktop_ready" not in maintenance.calls
    assert "modify_entitlement" not in maintenance.calls
    assert "tag_desktop" not in maintenance.calls


@pytest.mark.parametrize("observed", [False, True, "restart"])
async def test_accepted_rebuild_requires_real_observation_loop(maintenance, monkeypatch, observed):
    await prepare(maintenance, "recycle")
    submissions, observations = [], []
    waiting, release = asyncio.Event(), asyncio.Event()

    async def submit(request):
        submissions.append(list(request.desktop_id))
        return SimpleNamespace(body=SimpleNamespace(request_id="fixture", rebuild_results=[
            SimpleNamespace(to_map=lambda: {"DesktopId": maintenance.record["desktop_id"], "Code": "Success"})]))

    original_describe = ecd.describe_desktop

    async def describe(desktop_id):
        info = await original_describe(desktop_id)
        state = "Rebuilding" if observed and not observations else "Running"
        observations.append(state)
        if observed == "restart" and len(observations) == 2:
            waiting.set()
            await release.wait()
        return {**info, "status": state}

    async def bounded_wait(*args, **kwargs):
        # Run the real cloud observation loop, replacing only its time budget
        # and the explicit remote transport above; callback/SQL remain real.
        return await actual_wait(*args, **{**kwargs, "timeout_sec": 0.2, "poll_interval": 0})

    monkeypatch.setattr(ecd, "rebuild_desktop", actual_rebuild)
    monkeypatch.setattr(ecd, "ecd_client", lambda: SimpleNamespace(rebuild_desktops_async=submit))
    monkeypatch.setattr(ecd, "describe_desktop", describe)
    monkeypatch.setattr(ecd, "wait_desktop_ready", bounded_wait)
    if observed == "restart":
        task = asyncio.create_task(run(maintenance, "recycle"))
        try:
            await asyncio.wait_for(waiting.wait(), 4)
            assert (await desktops.get(maintenance.record["id"]))["status"] == "rebuild_observed"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    if observed:
        await run(maintenance, "recycle")
        assert (await desktops.get(maintenance.record["id"]))["pool_state"] == "prewarm"
        assert observations[:2] == ["Rebuilding", "Running"]
    else:
        with pytest.raises(TimeoutError, match="unconfirmed"):
            await run(maintenance, "recycle")
        incomplete = await desktops.get(maintenance.record["id"])
        assert incomplete["status"] == "rebuild_accepted" and incomplete["pool_state"] == "recycling"
        with pytest.raises(TimeoutError, match="unconfirmed"):
            await run(maintenance, "recycle")
        assert all(state == "Running" for state in observations)
        assert "modify_entitlement" not in maintenance.calls
    assert len(submissions) == 1


async def test_late_free_inventory_cannot_fill_a_successor_row(maintenance):
    await prepare(maintenance, "free")
    await desktops.update(maintenance.record["id"], desktop_id=None)
    async with get_db_session() as db:
        await db.execute(update(DesktopActivation).where(DesktopActivation.workspace_id == maintenance.workspace)
            .values(purchase_kind="create", step="suspended"))
    maintenance.pause = "inventory"
    maintenance.release.clear()
    task = asyncio.create_task(run(maintenance, "free"))
    try:
        await asyncio.wait_for(maintenance.reached.wait(), 4)
        successor = await replace(maintenance)
        maintenance.release.set()
        assert await asyncio.wait_for(task, 4) is False
        assert await desktops.get(successor["id"]) == successor
        assert "modify_entitlement" not in maintenance.calls
    finally:
        maintenance.release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_runtime_loss_is_not_swallowed_into_repair(maintenance):
    await prepare(maintenance, "recycle")
    maintenance.pause = "runtime"
    maintenance.release.clear()
    task = asyncio.create_task(run(maintenance, "recycle"))
    try:
        await asyncio.wait_for(maintenance.reached.wait(), 4)
        successor = await replace(maintenance)
        before = len(maintenance.calls)
        maintenance.release.set()
        with pytest.raises(channel.ChannelVerificationStopped):
            await asyncio.wait_for(task, 4)
        assert maintenance.calls[before:] == []
        assert await desktops.get(successor["id"]) == successor
    finally:
        maintenance.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("with_successor", [False, True])
async def test_background_end_user_cleanup_rechecks_tombstone_and_successor(maintenance, monkeypatch, with_successor):
    await prepare(maintenance, "ghost")
    end_user = "obx-" + maintenance.workspace[-12:]
    await desktops.update(maintenance.record["id"], end_user_id=end_user)
    original_describe = ecd.describe_desktop
    original_cleanup = ecd._remove_end_users_with_retry
    entered, release = asyncio.Event(), asyncio.Event()
    tasks, submissions = [], []

    async def describe(*args, **kwargs):
        return {**await original_describe(*args, **kwargs), "end_user_ids": [end_user]}

    async def deleted(_request):
        return SimpleNamespace(body=None)

    async def remove(request):
        submissions.append(list(request.users))
        entered.set()
        await release.wait()
        if with_successor:
            raise RuntimeError("Used in some region")
        return SimpleNamespace(body=None)

    async def cleanup(*args, **kwargs):
        tasks.append(asyncio.current_task())
        await original_cleanup(*args, **kwargs)

    monkeypatch.setattr(ecd, "delete_desktop", actual_delete)
    monkeypatch.setattr(ecd, "describe_desktop", describe)
    monkeypatch.setattr(ecd, "_remove_end_users_with_retry", cleanup)
    monkeypatch.setattr(ecd, "ecd_client", lambda: SimpleNamespace(delete_desktops_async=deleted))
    monkeypatch.setattr(ecd, "eds_user_client", lambda: SimpleNamespace(remove_users_async=remove))
    try:
        await run(maintenance, "ghost")
        await asyncio.wait_for(entered.wait(), 4)
        assert await desktops.get(maintenance.record["id"]) is None
        successor = None
        if with_successor:
            successor = await desktops.create(maintenance.workspace, maintenance.config.wuying_region_id,
                desktop_id="new-" + maintenance.record["desktop_id"], end_user_id=end_user,
                pool_state="assigned", tunnel_state="up", user_id=maintenance.owner)
            successor = await desktops.get(successor["id"])
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 4)
        assert submissions == [[end_user]]
        if successor:
            assert await desktops.get(successor["id"]) == successor
            # The final synthetic successor is not left eligible for later cases.
            await desktops.update(successor["id"], pool_state="retired", workspace_id=None)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_lost_admin_binding_uses_existing_http_refusal_mapping(maintenance):
    import httpx
    from fastapi import FastAPI
    from api import admin_fleet
    app = FastAPI()
    app.include_router(admin_fleet.router)
    app.dependency_overrides[admin_fleet.require_admin] = lambda: {"user_id": maintenance.owner}
    app.dependency_overrides[admin_fleet.get_workspace] = lambda: maintenance.workspace
    maintenance.pause = "modify_entitlement"
    maintenance.release.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local-fixture") as client:
        task = asyncio.create_task(client.post("/api/admin/fleet/desktops/" + maintenance.record["desktop_id"] + "/release"))
        try:
            await asyncio.wait_for(maintenance.reached.wait(), 4)
            await replace(maintenance)
            maintenance.release.set()
            response = await asyncio.wait_for(task, 4)
            assert response.status_code == 422
            assert "original binding" in response.text
        finally:
            maintenance.release.set()
            await asyncio.gather(task, return_exceptions=True)
