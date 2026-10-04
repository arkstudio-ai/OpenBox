"""Physical identity, ledger send admission and drainage across SQL workers."""
import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from agent import effect_ledger as effects
from assistant import resource_control as controls
from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.cloud_desktop import CloudDesktop
from db.models.external_effect import ExternalEffect
from db.models.resource_control import ResourceControlLease
from db.models.workspace import WorkspaceMember
from question import runtime
from session.internal_parts import begin_session_write
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_steering import running


@pytest.fixture
async def resource():
    scope, created, lease, _ = await running()
    stamp = runtime.now()
    desktop_id = "physical-" + created["task_id"]
    async with get_db_session() as db:
        db.add(CloudDesktop(id="desktop-" + created["task_id"], desktop_id=desktop_id,
            workspace_id=scope["workspace_id"], user_id=scope["user_id"], region_id="cn-test",
            status="running", pool_state="assigned", created_at=stamp, updated_at=stamp))
    enrollment = dict(desktop_id=desktop_id, workspace_id=scope["workspace_id"], user_id=scope["user_id"])
    fence = await controls.enroll_desktop(**enrollment)
    run = effects.EffectRunFence(lease.session_id, lease.user_id, lease.run_id, lease.generation)
    yield fence, run, lease, enrollment
    await lease.release(session_status="idle")


async def prepare(resource, *, key="operation", fence=None):
    control, run, _, _ = resource
    return await effects.prepare_effect(run, adapter="resource-test", provider="local-test", operation="input",
        logical_key=key, request_payload={"key": key}, resource_fence=fence or control)


async def close(resource):
    control, run, _, _ = resource
    async with get_db_session() as db:
        await begin_session_write(db)
        return await controls.close_admission_locked(db, control, user_id=run.tenant_id)


async def drain(resource):
    async with get_db_session() as db:
        await begin_session_write(db)
        row = await controls.locked(db, resource[0].resource_id)
        return await controls.drain_status_locked(db, row)


async def test_two_callers_share_one_physical_identity_and_cannot_enroll_an_unowned_machine(resource):
    control, _, _, enrollment = resource
    left, right = await asyncio.gather(*(controls.enroll_desktop(**enrollment) for _ in range(2)))
    assert left == right == control
    with pytest.raises(AssistantError):
        await controls.enroll_desktop(**{**enrollment, "desktop_id": "unrelated-machine"})
    async with get_db_session() as db:
        assert len(list((await db.scalars(select(ResourceControlLease).where(
            ResourceControlLease.workspace_id == enrollment["workspace_id"]))).all())) == 1


async def test_prepared_operation_arriving_after_control_close_cannot_cross_send_boundary(resource):
    prepared = await prepare(resource)
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, resource[1])
    await close(resource)
    with pytest.raises(AssistantError):
        await effects.mark_effect_submitting(claim)
    async with get_db_session() as db:
        row = await db.get(ExternalEffect, prepared.snapshot.effect_id)
        assert row.state == "prepared" and row.attempt_count == 0 and row.submitting_at is None
    assert (await drain(resource))["tracked_operations_drained"]
    assert not (await drain(resource))["remote_exclusivity_verified"]


async def test_old_resource_epoch_cannot_be_relabelled_as_a_new_operation(resource):
    prepared = await prepare(resource)
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, resource[1])
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, resource[0].resource_id)
        row.epoch += 1
    with pytest.raises(AssistantError):
        await effects.mark_effect_submitting(claim)
    with pytest.raises(effects.EffectConflictError):
        await prepare(resource, fence=replace(resource[0], epoch=2))


@pytest.mark.parametrize("state", ["submitting", "accepted", "outcome_unknown", "manual_review", "succeeded", "failed"])
async def test_only_confirmed_terminal_operations_drain_not_timeouts_or_manual_review(resource, state):
    prepared = await prepare(resource)
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, resource[1])
    await effects.mark_effect_submitting(claim)
    if state == "accepted":
        await effects.record_effect_accepted(claim, provider_handle="local-receipt", receipt={"accepted": True})
    elif state == "outcome_unknown":
        await effects.record_effect_outcome_unknown(claim, error={"code": "response_lost"})
    elif state in {"manual_review", "succeeded", "failed"}:
        await effects.settle_effect(claim, state=state, receipt={"terminal": state})
    await close(resource)
    async with get_db_session() as db:
        effect = await db.get(ExternalEffect, prepared.snapshot.effect_id)
        if effect.claim_expires_at:
            effect.claim_expires_at = runtime.now() - timedelta(seconds=120)
    await resource[2].release(session_status="idle")
    status = await drain(resource)
    assert status["tracked_operations_drained"] == (state in {"succeeded", "failed"})
    assert status["blocking_effect_ids"] == ([] if state in {"succeeded", "failed"} else [prepared.snapshot.effect_id])
    assert not status["remote_exclusivity_verified"]


async def test_control_close_and_operation_admission_serialize_across_independent_connections(resource):
    prepared = await prepare(resource)
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, resource[1])
    admitted, closed = await asyncio.wait_for(asyncio.gather(effects.mark_effect_submitting(claim),
        close(resource), return_exceptions=True), timeout=10)
    assert not isinstance(closed, Exception)
    status = await drain(resource)
    if isinstance(admitted, Exception):
        assert isinstance(admitted, AssistantError)
        assert status["tracked_operations_drained"]
    else:
        assert status["blocking_effect_ids"] == [prepared.snapshot.effect_id]
        with pytest.raises(AssistantError):
            await effects.assert_effect_dispatchable(claim)


async def test_revoked_membership_invalidates_an_already_prepared_resource_operation(resource):
    prepared = await prepare(resource)
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, resource[1])
    async with get_db_session() as db:
        member = await db.get(WorkspaceMember, (resource[3]["workspace_id"], resource[1].tenant_id))
        member.status = "removed"
    with pytest.raises(AssistantError):
        await effects.mark_effect_submitting(claim)


async def test_human_expiry_is_durable_hold_and_late_heartbeat_cannot_restore_control(resource):
    control, run, _, _ = resource
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, control.resource_id)
        row.owner_kind, row.owner_id = "human", run.tenant_id
        row.epoch += 1
        row.expires_at = runtime.now() - timedelta(seconds=1)
    human = replace(control, owner_kind="human", owner_id=run.tenant_id, epoch=2)
    status = await controls.heartbeat(fence=human, user_id=run.tenant_id)
    assert status["status"] == "hold" and status["admission_state"] == "closed"
    assert await controls.expire_leases() == 0
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, control.resource_id)
        assert controls.fence_for(row) == human
    with pytest.raises(AssistantError):
        await prepare(resource, key="after-expiry")


async def test_expiry_scanner_never_returns_control_to_automation(resource):
    control, run, _, _ = resource
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, control.resource_id)
        row.owner_kind, row.owner_id = "human", run.tenant_id
        row.expires_at = runtime.now() - timedelta(seconds=1)
    assert await controls.expire_leases() >= 1
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, control.resource_id)
        assert row.status == "hold" and row.owner_kind == "human" and row.admission_state == "closed"
