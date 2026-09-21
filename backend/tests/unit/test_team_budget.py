from decimal import Decimal
from datetime import timedelta

import pytest

from sqlalchemy import select

from core.identifier import ascending
from db.base import get_db_session
from db.models.billing import UsageEvent
from db.models.agent_driver import AgentDriverState
from db.models.team import TeamRun
from team.budget import model_usage
from team.journal import utcnow
from tests.unit.test_team_commands import setup_team


async def test_pending_concurrent_calls_are_not_unpriced_finished_calls():
    run_id, actor, _, root, members = await setup_team()
    async with get_db_session() as db:
        db.add(AgentDriverState(session_id=root, user_id=actor.owner_user_id, generation=7,
            run_id="live-budget", phase="running", lease_expires_at=utcnow() + timedelta(minutes=5),
            started_at=utcnow(), updated_at=utcnow()))
        for status, cost, member in (("pending", None, root), ("shadow", Decimal("1.25"), members[0])):
            identifier = ascending("usage")
            db.add(UsageEvent(id=identifier, idempotency_key=identifier, workspace_id=actor.workspace_id,
                user_id=actor.owner_user_id, session_id=member, session_title="Team budget test",
                model_id="openai/test", kind="chat", tokens={}, total_tokens=0, credits=cost,
                status=status, pricing={"request_fence": {"run_id": "live-budget", "generation": 7}}, created_at=utcnow()))
    async with get_db_session() as db:
        run = await db.get(TeamRun, run_id)
        assert await model_usage(db, run, [root, *members]) == (Decimal("1.25"), 0)
        pending = await db.scalar(select(UsageEvent).where(UsageEvent.session_id == root))
        pending.status = "unreported"
    async with get_db_session() as db:
        assert await model_usage(db, await db.get(TeamRun, run_id), [root, *members]) == (Decimal("1.25"), 1)


@pytest.mark.parametrize("lost", ["idle", "expired", "replacement", "wrong_run", "legacy", "no_driver"])
async def test_pending_without_exact_live_lease_still_blocks_unknown_cost(lost):
    run_id, actor, _, root, members = await setup_team()
    identifier = ascending("usage")
    async with get_db_session() as db:
        if lost != "no_driver":
            db.add(AgentDriverState(session_id=root, user_id=actor.owner_user_id,
                generation=8 if lost == "replacement" else 7,
                run_id="different-run" if lost == "wrong_run" else "old-budget",
                phase="idle" if lost == "idle" else "running",
                lease_expires_at=utcnow() + timedelta(minutes=-5 if lost == "expired" else 5),
                started_at=utcnow(), updated_at=utcnow()))
        db.add(UsageEvent(id=identifier, idempotency_key=identifier, workspace_id=actor.workspace_id,
            user_id=actor.owner_user_id, session_id=root, session_title="Team crashed meter",
            model_id="openai/test", kind="chat", tokens={}, total_tokens=0, credits=None,
            status="pending", pricing={} if lost == "legacy" else {
                "request_fence": {"run_id": "old-budget", "generation": 7}}, created_at=utcnow()))
    async with get_db_session() as db:
        assert await model_usage(db, await db.get(TeamRun, run_id), [root, *members]) == (Decimal(0), 1)
        # Detecting the orphan does not fabricate a zero charge or rewrite it.
        event = await db.get(UsageEvent, identifier)
        assert event.status == "pending" and event.credits is None


async def test_real_meter_persists_its_driver_identity_through_settlement(monkeypatch):
    from agent.driver import bind_current_lease, reserve_run, reset_current_lease
    from billing.service import UsageMeter
    monkeypatch.setenv("BILLING_MODE", "shadow")
    run, actor, _, root, _ = await setup_team()
    from team import runtime_binding
    binding_token = runtime_binding._current.set(runtime_binding.RuntimeBinding(
        run, root, actor.owner_user_id, actor.workspace_id, "", "coordinator", {}, {}))
    lease = await reserve_run(root, actor.owner_user_id, run_id="fenced-meter")
    # The shared in-memory SQLite fixture has one physical connection. Lease
    # ownership is real; its independent renewal poller is not under test here.
    await lease.stop_monitor()
    token = bind_current_lease(lease)
    try:
        meter = await UsageMeter.start(model_id="openai/test", session_id=root, user_id=actor.owner_user_id)
        async with get_db_session() as db:
            event = await db.get(UsageEvent, meter.event_id)
            assert event.pricing["request_fence"] == {"run_id": lease.run_id, "generation": lease.generation}
            assert event.pricing["team_attribution"]["run_id"] == run
            assert event.pricing["team_attribution"]["category"] == "coordinator"
        await meter.finish({"input": 2, "output": 3})
        async with get_db_session() as db:
            event = await db.get(UsageEvent, meter.event_id)
            assert event.pricing["request_fence"] == {"run_id": lease.run_id, "generation": lease.generation}
            assert event.status != "pending"
            assert event.pricing["team_attribution"]["category"] == "coordinator"
    finally:
        reset_current_lease(token)
        runtime_binding._current.reset(binding_token)
        await lease.release(session_status="idle")



@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
async def test_model_admission_has_no_team_cap_or_max_context_headroom(monkeypatch, mode):
    from types import SimpleNamespace
    from team import budget, runtime_binding
    from team.journal import snapshot
    monkeypatch.setenv("BILLING_MODE", mode)
    run, actor, _, root, _ = await setup_team()
    before = await snapshot(run, actor)
    token = runtime_binding._current.set(runtime_binding.RuntimeBinding(
        run, root, actor.owner_user_id, actor.workspace_id, "", "coordinator", {}, {}))
    try:
        # Even an unpriced model uses the ordinary billing gate; no speculative
        # maximum-context cost can impose a second limit on a team.
        assert await budget.before_model_call(SimpleNamespace(session_id=root),
            model_id="fixture/unpriced", output_tokens=1000000) is None
    finally:
        runtime_binding._current.reset(token)
    assert await snapshot(run, actor) == before


@pytest.mark.parametrize("code,reason", [("INSUFFICIENT_CREDITS", "insufficient_credits"), ("MODEL_UNPRICED", "model_unpriced")])
async def test_account_billing_failure_pauses_only_bound_team_once(monkeypatch, code, reason):
    from types import SimpleNamespace
    from billing.service import BillingError
    from team import budget, runtime_binding
    from team.errors import TeamExecutionPaused
    from team.journal import snapshot
    monkeypatch.setattr("team.scheduler.schedule", lambda *_: None)
    run, actor, _, root, _ = await setup_team()
    other, other_actor, *_ = await setup_team()
    other_before = await snapshot(other, other_actor)
    token = runtime_binding._current.set(runtime_binding.RuntimeBinding(
        run, root, actor.owner_user_id, actor.workspace_id, "", "coordinator", {}, {}))
    try:
        ctx = SimpleNamespace(session_id=root, message_id="billing-fixture")
        for _ in range(2):
            with pytest.raises(TeamExecutionPaused):
                await budget.handle_billing_error(ctx, BillingError(code, "Billing refused"))
        state = await snapshot(run, actor)
        assert state["run"]["state"] == "pausing" and state["run"]["pause_reason"] == reason
        assert state == await snapshot(run, actor, rebuild=True)
        assert await snapshot(other, other_actor) == other_before
    finally:
        runtime_binding._current.reset(token)


def test_legacy_budgets_are_readable_but_absent_from_new_contract():
    from agent_catalog.schemas import TeamPolicy
    original = {"budget_credits": "0.01", "paid_tools": {"image_gen": {"per_call": "1", "total": "2"}}}
    policy = TeamPolicy.model_validate(original)
    assert "budget_credits" not in policy.model_dump()
    assert "budget_credits" not in TeamPolicy.model_json_schema()["properties"]
    assert policy.model_dump()["paid_tools"] == {"image_gen": {"authorized": True}}
    assert original["budget_credits"] == "0.01"


async def test_coordinator_members_and_other_teams_share_only_the_account_ledger(monkeypatch):
    from billing.service import BillingError, UsageMeter, lock_balance
    from team import runtime_binding
    from db.models.billing import CreditLedger
    from unittest.mock import AsyncMock
    monkeypatch.setenv("BILLING_MODE", "enforce")
    monkeypatch.setattr("billing.subscriptions.ensure_period_allowance", AsyncMock())
    run, actor, _, root, members = await setup_team()
    second, _, _, second_root, _ = await setup_team(scope=actor)
    foreign, foreign_actor, _, foreign_root, _ = await setup_team()
    async with get_db_session() as db:
        (await lock_balance(db, actor.workspace_id)).balance = Decimal("1")
        (await lock_balance(db, foreign_actor.workspace_id)).balance = Decimal("10")
    token = runtime_binding._current.set(runtime_binding.RuntimeBinding(
        run, root, actor.owner_user_id, actor.workspace_id, "", "coordinator", {}, {}))
    try:
        meter = await UsageMeter.start(model_id="openai/qwen3.8-flash", session_id=root, user_id=actor.owner_user_id)
        # The exact account debit occurs once, even if the provider settlement
        # is retried. An in-flight request may exhaust the account as in chat.
        await meter.finish({"input": 1000000, "output": 1000000})
        await meter.finish({"input": 1000000, "output": 1000000})
        for sid in (members[0], second_root):
            with pytest.raises(BillingError) as error:
                await UsageMeter.start(model_id="openai/qwen3.8-flash", session_id=sid, user_id=actor.owner_user_id)
            assert error.value.code == "INSUFFICIENT_CREDITS"
        async with get_db_session() as db:
            assert (await lock_balance(db, actor.workspace_id)).balance == Decimal("-2.5")
            rows = list((await db.scalars(select(CreditLedger).where(CreditLedger.workspace_id == actor.workspace_id))).all())
            assert len(rows) == 1 and rows[0].amount == Decimal("-3.5")
        independent = await UsageMeter.start(model_id="openai/qwen3.8-flash", session_id=foreign_root, user_id=foreign_actor.owner_user_id)
        assert independent.workspace_id == foreign_actor.workspace_id
        await independent.finish({"input": 1, "output": 1})
    finally:
        runtime_binding._current.reset(token)
