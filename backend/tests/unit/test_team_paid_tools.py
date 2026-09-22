"""Real journal admission and existing ledger reconciliation for paid jobs."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
import uuid

import pytest

from billing.media import MediaQuote
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.billing import UsageEvent
from db.models.team import TeamRun
from db.models.video_job import VideoJob
from team import commands, paid_tools, runtime_binding
from team.errors import TeamError
from team.journal import command, snapshot, utcnow
from tests.unit.test_team_commands import make_task, setup_team
from tests.unit.test_subagent_composition import _config
from tool.tool import ToolContext


@pytest.fixture
async def paid(monkeypatch):
    from billing import service
    from team import model_limits
    from core import config as core_config
    config = _config("openai/test")
    config.team_tools_enabled = True
    config.team_wake_debounce_seconds = 0
    config.team_stall_seconds = 600
    monkeypatch.setattr(core_config, "get_config", lambda: config)
    monkeypatch.setattr(runtime_binding, "get_config", lambda: config)
    monkeypatch.setattr(service, "billing_mode", lambda: "shadow")
    # Protocol fixtures use a fictitious model; pricing-bound tests exercise
    # the real calculation against an explicit fixture tariff separately.
    monkeypatch.setattr(model_limits, "model_bound", lambda *_, **__: SimpleNamespace(credits=Decimal("0.1")))
    run, actor, server, root, members = await setup_team()
    task = await make_task(run, server, members[0])
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    state = await snapshot(run, actor)
    project = state["run"]["project_id"]
    attempt = next(iter(state["attempts"].values()))
    async def grant(writer):
        writer.append("team.grant", "grant", {"id": run, **writer.state["grant"], "version": 2,
            "delegable_tools": ["video_generate"], "paid_tools": {"video_generate": {"per_call": "6", "total": "10"}}})
        writer.append("team.attempt", "attempt", {**attempt, "driver_run_id": "driver-1", "generation": 1})
        return {"granted": True}
    await command(run, server, "grant", {}, grant)
    async with get_db_session() as db:
        db.add(AgentDriverState(session_id=members[0], user_id=actor.owner_user_id, run_id="driver-1", generation=1,
            phase="running", lease_expires_at=utcnow() + timedelta(minutes=10), updated_at=utcnow()))
    binding = runtime_binding.RuntimeBinding(run, members[0], actor.owner_user_id, actor.workspace_id, project,
        "member", {"tool_allowlist": ["video_generate"], "mcp_refs": []}, {})
    token = runtime_binding._current.set(binding)
    ctx = ToolContext(session_id=members[0], user_id=actor.owner_user_id, workspace_id=actor.workspace_id,
        project_id=project, part_id="part-1", run_id="driver-1", run_generation=1)
    try:
        yield SimpleNamespace(run=run, actor=actor, server=server, ctx=ctx, attempt=attempt, config=config)
    finally:
        runtime_binding._current.reset(token)


async def job_for(paid, *, suffix=None):
    key = suffix or uuid.uuid4().hex
    job = VideoJob(id="video-" + key, user_id=paid.actor.owner_user_id, session_id=paid.ctx.session_id,
        project_id=paid.ctx.project_id, kind="segment", idempotency_key=key, request_hash="a" * 64,
        status="dispatching", request_data={}, result_data={}, created_at=utcnow(), updated_at=utcnow())
    async with get_db_session() as db:
        db.add(job)
    return job


def price(credits="6"):
    return MediaQuote("video-test", "720p", 5, Decimal(credits) if credits is not None else None,
        {"version": "test-v1", "model": "video-test", "price_bound_verified": True})


async def reserve(paid, job, **kwargs):
    return await paid_tools.reserve(kwargs.pop("ctx", paid.ctx), "video_generate", kwargs.pop("price", price()),
        external_kind="video_job", external_id=job.id, billing_keys=["generate:" + job.id], **kwargs)


async def test_atomic_effect_tracking_uses_stable_call_without_team_limits(paid):
    one, two = await job_for(paid), await job_for(paid)
    outcomes = await asyncio.gather(reserve(paid, one), reserve(paid, two, ctx=replace(paid.ctx, part_id="part-2")), return_exceptions=True)
    assert all(isinstance(value, paid_tools.Reservation) for value in outcomes)
    index = next(i for i, value in enumerate(outcomes) if isinstance(value, paid_tools.Reservation))
    ctx = paid.ctx if index == 0 else replace(paid.ctx, part_id="part-2")
    before = await snapshot(paid.run, paid.actor)
    replayed = await reserve(paid, (one, two)[index], ctx=ctx)
    assert replayed.id == outcomes[index].id
    assert (await snapshot(paid.run, paid.actor))["seq"] == before["seq"]
    assert len(before["reservations"]) == 2


async def test_unpriced_and_foreign_job_are_refused_before_dispatch(paid):
    job = await job_for(paid)
    with pytest.raises(TeamError) as error:
        await reserve(paid, job, price=price(None))
    assert error.value.code == "PERMISSION_REQUIRES_USER"
    async with get_db_session() as db:
        (await db.get(VideoJob, job.id)).session_id = "foreign-session"
    with pytest.raises(TeamError) as error:
        await reserve(paid, job)
    assert error.value.code == "INVALID_RESERVATION"
    assert not (await snapshot(paid.run, paid.actor))["reservations"]


@pytest.mark.parametrize("verified", [None, False])
async def test_placeholder_price_is_not_a_verified_paid_upper_bound(paid, verified):
    job = await job_for(paid)
    unverified = price()
    if verified is None:
        unverified.snapshot.pop("price_bound_verified")
    else:
        unverified.snapshot["price_bound_verified"] = verified
    with pytest.raises(TeamError, match="verified price"):
        await reserve(paid, job, price=unverified)
    assert not (await snapshot(paid.run, paid.actor))["reservations"]


async def test_unknown_job_retains_budget_and_prevents_premature_submission(paid):
    job = await job_for(paid)
    ticket = await reserve(paid, job)
    async def submit(writer):
        task = writer.state["tasks"][paid.attempt["task_id"]]
        return await commands.update_task(writer, task["id"], task["revision"], "submit", summary="Done")
    with pytest.raises(TeamError) as error:
        await command(paid.run, paid.server, "premature", {}, submit)
    assert error.value.code == "EXTERNAL_WORK_PENDING"
    async with get_db_session() as db:
        (await db.get(VideoJob, job.id)).status = "failed"
    assert not await paid_tools.reconcile(paid.run, paid.actor)
    assert (await snapshot(paid.run, paid.actor))["reservations"][ticket.id]["state"] == "reserved"
    # A real settled usage fact plus a terminal external result resolves the
    # reservation even after the member's generation stops. No new debit.
    usage_id = "usage-" + uuid.uuid4().hex
    async with get_db_session() as db:
        (await db.get(VideoJob, job.id)).status = "completed"
        db.add(UsageEvent(id=usage_id, idempotency_key="generate:" + job.id, user_id=paid.actor.owner_user_id,
            workspace_id=paid.actor.workspace_id, session_id=paid.ctx.session_id, session_title="Paid test",
            model_id="video-test", kind="video_generate", tokens={}, total_tokens=0,
            credits=Decimal("4.25"), status="shadow", pricing={"version": "test-v1"}, created_at=utcnow()))
    assert await paid_tools.reconcile(paid.run, paid.actor)
    state = await snapshot(paid.run, paid.actor)
    assert state["reservations"][ticket.id]["settled_amount"] == "4.250000000000"
    assert state["reservations"][ticket.id]["billing_ref"] == usage_id
    assert not await paid_tools.reconcile(paid.run, paid.actor)
    await command(paid.run, paid.server, "completed", {}, submit)


async def test_stopped_paid_attempt_is_unknown_until_proven_not_dispatched(paid):
    from team.scheduler import tick
    from db.models.agent_inbox import AgentInboxItem
    from sqlalchemy import select
    job = await job_for(paid)
    ticket = await reserve(paid, job)
    async with get_db_session() as db:
        (await db.get(AgentDriverState, paid.ctx.session_id)).phase = "idle"
        # No accepted input remains after the stopped turn.
        for row in (await db.execute(select(AgentInboxItem).where(AgentInboxItem.session_id == paid.ctx.session_id))).scalars():
            row.state = "canceled"
            row.canceled_at = utcnow()
    await tick(paid.run, paid.actor)
    state = await snapshot(paid.run, paid.actor)
    assert state["attempts"][paid.attempt["id"]]["state"] == "outcome_unknown"
    assert state["reservations"][ticket.id]["state"] == "reserved"
    async with get_db_session() as db:
        row = await db.get(VideoJob, job.id)
        row.status = "cancelled"
        row.request_data = {"_team_not_dispatched": True}
    assert await paid_tools.reconcile(paid.run, paid.actor)
    state = await snapshot(paid.run, paid.actor)
    assert state["reservations"][ticket.id]["state"] == "released"
    assert state["attempts"][paid.attempt["id"]]["state"] == "failed"
    assert state["tasks"][paid.attempt["task_id"]]["state"] == "failed"


async def test_settlement_uses_reserved_tariff_when_catalogue_changes(paid):
    from db.models.session import Session
    from team.paid_pricing import frozen_media_quote
    job = await job_for(paid)
    original = MediaQuote("video-test", "720p", 5, Decimal("5"),
        {"version": "old-v1", "model": "video-test", "per_second": "1", "price_bound_verified": True})
    await reserve(paid, job, price=original)
    changed = MediaQuote("video-test", "720p", 5, Decimal("45"),
        {"version": "new-v2", "model": "video-test", "per_second": "9"})
    async with get_db_session() as db:
        session = await db.get(Session, paid.ctx.session_id)
        billed = await frozen_media_quote(db, session, "generate:" + job.id, changed,
            kind="video_generate", tokens={"duration_sec": 5})
    assert billed.credits == Decimal("5") and billed.snapshot["version"] == "old-v1"
    assert billed.snapshot["team_attribution"]["category"] == "member_work"
    assert billed.snapshot["team_attribution"]["attempt_id"] == paid.attempt["id"]


@pytest.mark.parametrize("count", [0, -1, True, 1.5, 17])
async def test_reservation_requires_a_finite_positive_meter_count(paid, count):
    job = await job_for(paid)
    with pytest.raises(TeamError) as error:
        await reserve(paid, job, expected_usage_count=count)
    assert error.value.code == "INVALID_RESERVATION"
    assert not (await snapshot(paid.run, paid.actor))["reservations"]


async def add_usage(paid, job, *, credits="4", session_id=None):
    async with get_db_session() as db:
        db.add(UsageEvent(id="usage-" + uuid.uuid4().hex, idempotency_key="generate:" + job.id,
            user_id=paid.actor.owner_user_id, workspace_id=paid.actor.workspace_id,
            session_id=session_id or paid.ctx.session_id, session_title="Paid fault test",
            model_id="video-test", kind="video_generate", tokens={}, total_tokens=0,
            credits=Decimal(credits), status="shadow", pricing={"version": "test-v1"}, created_at=utcnow()))
        (await db.get(VideoJob, job.id)).status = "completed"


async def test_price_bound_violation_pauses_without_releasing_or_rebilling(paid):
    job = await job_for(paid)
    ticket = await reserve(paid, job)
    await add_usage(paid, job, credits="7")
    assert await paid_tools.reconcile(paid.run, paid.actor)
    state = await snapshot(paid.run, paid.actor)
    assert state["run"]["state"] == "pausing"
    assert state["run"]["pause_reason"] == "paid_price_bound_exceeded"
    assert state["reservations"][ticket.id]["state"] == "reserved"
    assert state["notices"][-1]["code"] == "PAID_PRICE_BOUND_EXCEEDED"
    # Reconciliation is a stable journal receipt, never a second charge.
    seq = state["seq"]
    await paid_tools.reconcile(paid.run, paid.actor)
    assert (await snapshot(paid.run, paid.actor))["seq"] == seq


async def test_incomplete_or_foreign_meters_cannot_resolve_paid_work(paid):
    job = await job_for(paid)
    ticket = await reserve(paid, job, expected_usage_count=2)
    await add_usage(paid, job)
    assert not await paid_tools.reconcile(paid.run, paid.actor)
    assert (await snapshot(paid.run, paid.actor))["reservations"][ticket.id]["state"] == "reserved"

    other = await job_for(paid)
    # A separate authorized operation has independent effect tracking.
    ticket = await reserve(paid, other, ctx=replace(paid.ctx, part_id="other"), price=price("3"))
    await add_usage(paid, other, credits="2", session_id="foreign-session")
    assert not await paid_tools.reconcile(paid.run, paid.actor)
    assert (await snapshot(paid.run, paid.actor))["reservations"][ticket.id]["state"] == "reserved"


async def test_fenced_effect_recovery_releases_only_proven_unsent_reservation(paid):
    from agent import effect_ledger as effects
    from db.models.external_effect import ExternalEffect
    from tests.unit.test_effect_ledger import _prepare

    async def grant(writer):
        writer.append("team.grant", "grant", {"id": paid.run, **writer.state["grant"], "version": 3,
            "delegable_tools": ["image_gen"], "paid_tools": {"image_gen": {"per_call": "6", "total": "10"}}})
        return {}
    await command(paid.run, paid.server, "image-grant", {}, grant)
    binding = runtime_binding.current_binding()
    token = runtime_binding._current.set(replace(binding, spec={**binding.spec, "tool_allowlist": ["image_gen"]}))
    try:
        fence = effects.EffectRunFence.from_tool_context(paid.ctx)
        prepared = await _prepare(fence, paid.ctx.project_id, adapter="image_gen")
        effect_id = prepared.snapshot.effect_id
        ticket = await paid_tools.reserve(paid.ctx, "image_gen", price(), external_kind="external_effect",
            external_id=effect_id, billing_keys=["image:" + paid.ctx.part_id])
        assert not await paid_tools.reconcile(paid.run, paid.actor)
        async with get_db_session() as db:
            driver = await db.get(AgentDriverState, paid.ctx.session_id)
            driver.phase = "idle"
            driver.lease_expires_at = utcnow() - timedelta(minutes=1)
            (await db.get(ExternalEffect, effect_id)).prepared_at = utcnow() - timedelta(hours=1)
        assert await effects.recover_effect_once(effect_id) == "failed_before_dispatch"
        assert await paid_tools.reconcile(paid.run, paid.actor)
        state = await snapshot(paid.run, paid.actor)
        assert state["reservations"][ticket.id]["state"] == "released"
        assert state["reservations"][ticket.id]["confirmed_not_dispatched"] is True
    finally:
        runtime_binding._current.reset(token)


async def test_later_provider_dispatch_invalidates_old_unsent_proof_without_trace(paid, monkeypatch):
    from agent.trajectory import capture_service_dispatch, service_scope
    from tool.media.video_production import _update_job
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    job = await job_for(paid)
    ticket = await reserve(paid, job)
    await paid_tools.refuse_job(job, TeamError("PERMISSION_REQUIRES_USER", "First attempt refused before send"))
    async with service_scope(paid.ctx, job=job):
        async with capture_service_dispatch(purpose="video_generation", provider="fixture", model="test",
                operation="submit", body={"prompt": "Owned fixture"}, profile="fixture"):
            # This is the provider boundary; no actual network request is made.
            pass
    # A stale pre-dispatch object may still reach an error handler. It must
    # not erase the newer durable dispatch evidence.
    await paid_tools.refuse_job(job, TeamError("PERMISSION_REQUIRES_USER", "Stale handler"))
    async with get_db_session() as db:
        current = await db.get(VideoJob, job.id)
        assert current.request_data["_team_provider_dispatches"] == 1
        assert current.request_data["_team_not_dispatched"] is False
    assert not await paid_tools.reconcile(paid.run, paid.actor)
    assert (await snapshot(paid.run, paid.actor))["reservations"][ticket.id]["state"] == "reserved"
    usage_id = "usage-" + uuid.uuid4().hex
    async with get_db_session() as db:
        db.add(UsageEvent(id=usage_id, idempotency_key="generate:" + job.id, user_id=paid.actor.owner_user_id,
            workspace_id=paid.actor.workspace_id, session_id=paid.ctx.session_id, session_title="Retry test",
            model_id="video-test", kind="video_generate", tokens={}, total_tokens=0,
            credits=Decimal("5"), status="shadow", pricing={"version": "test-v1"}, created_at=utcnow()))
    await _update_job(job.id, status="completed")
    assert await paid_tools.reconcile(paid.run, paid.actor)
    after = (await snapshot(paid.run, paid.actor))["reservations"][ticket.id]
    assert after["state"] == "settled" and Decimal(after["settled_amount"]) == 5
