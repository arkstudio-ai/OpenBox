"""Periodic Agent recovery converges work that expires after process startup."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from agent import recovery_service as service_module
from agent.driver import RecoveredDriver, get_driver_state, reserve_run
from agent.recovery import RepairResult
from agent.recovery_service import AgentRecoveryResult, AgentRecoveryService
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.message import Message
from db.models.project import Project
from db.models.session import Session
from db.models.user import User


@pytest.mark.asyncio
async def test_one_pass_scans_task_state_even_without_expired_records(monkeypatch):
    calls: list[tuple[str, object]] = []

    async def recover_records():
        calls.append(("drivers", None))
        return []

    async def recover_outboxes(records):
        calls.append(("outboxes", list(records)))
        return 0

    async def reconcile(records, **_kwargs):
        calls.append(("rejoin", list(records)))
        return []

    async def repair(records):
        calls.append(("repair", list(records)))
        return []

    async def resume(records):
        calls.append(("resume", list(records)))
        return [], []

    async def unbound():
        calls.append(("unbound", None))
        return []

    async def settle_inbox():
        calls.append(("inbox_settle", None))
        return 0

    async def resume_inbox():
        calls.append(("inbox_resume", None))
        return []

    async def recover_effects():
        from agent.effect_ledger import EffectRecoveryResult

        calls.append(("effects", None))
        return EffectRecoveryResult()

    monkeypatch.setattr("agent.driver.recover_expired_driver_records", recover_records)
    monkeypatch.setattr(
        "agent.task_handoff.recover_task_handoff_outboxes", recover_outboxes
    )
    monkeypatch.setattr("agent.recovery.reconcile_completed_task_handoffs", reconcile)
    monkeypatch.setattr("agent.recovery.repair_expired_sessions", repair)
    monkeypatch.setattr("agent.recovery.resume_reserved_prompts", resume)
    monkeypatch.setattr("agent.recovery.resume_unbound_task_children", unbound)
    monkeypatch.setattr("agent.inbox.settle_orphaned_claims", settle_inbox)
    monkeypatch.setattr("agent.inbox.resume_claimable_inbox_sessions", resume_inbox)
    monkeypatch.setattr(
        "agent.effect_ledger.recover_external_effects_once", recover_effects
    )

    result = await service_module.recover_agent_work_once()

    assert result == AgentRecoveryResult()
    assert [name for name, _value in calls] == [
        "drivers",
        "outboxes",
        "rejoin",
        "repair",
        "resume",
        "repair",
        "unbound",
        "inbox_settle",
        "inbox_resume",
        "effects",
    ]
    assert calls[1][1] == []
    assert calls[-2:] == [("inbox_resume", None), ("effects", None)]


@pytest.mark.asyncio
async def test_one_pass_uses_full_record_and_strict_recovery_order(monkeypatch):
    record = RecoveredDriver(
        session_id="session-1",
        user_id="user-1",
        run_id="run-1",
        generation=4,
        phase="running",
        trigger_message_id="message-1",
    )
    calls: list[tuple[str, object]] = []

    async def recover_records():
        calls.append(("drivers", None))
        return [record]

    async def recover_outboxes(records):
        calls.append(("outboxes", list(records)))
        return 1

    async def reconcile(records, **_kwargs):
        calls.append(("rejoin", list(records)))
        return []

    async def repair(records):
        calls.append(("repair", list(records)))
        return [RepairResult(session_id=item.session_id) for item in records]

    async def resume(records):
        calls.append(("resume", list(records)))
        return [], []

    async def unbound():
        calls.append(("unbound", None))
        return []

    async def settle_inbox():
        calls.append(("inbox_settle", None))
        return 0

    async def resume_inbox():
        calls.append(("inbox_resume", None))
        return []

    monkeypatch.setattr("agent.driver.recover_expired_driver_records", recover_records)
    monkeypatch.setattr(
        "agent.task_handoff.recover_task_handoff_outboxes", recover_outboxes
    )
    monkeypatch.setattr("agent.recovery.reconcile_completed_task_handoffs", reconcile)
    monkeypatch.setattr("agent.recovery.repair_expired_sessions", repair)
    monkeypatch.setattr("agent.recovery.resume_reserved_prompts", resume)
    monkeypatch.setattr("agent.recovery.resume_unbound_task_children", unbound)
    monkeypatch.setattr("agent.inbox.settle_orphaned_claims", settle_inbox)
    monkeypatch.setattr("agent.inbox.resume_claimable_inbox_sessions", resume_inbox)

    result = await service_module.recover_agent_work_once()

    assert calls[1] == ("outboxes", [record])
    assert calls[2] == ("rejoin", [record])
    assert calls[3] == ("repair", [record])
    assert result.expired_markers == 1
    assert result.completed_outboxes == 1
    assert result.repaired_sessions == 1


@pytest.mark.asyncio
async def test_recovery_pass_starts_only_quota_slots_and_keeps_exact_markers(
    monkeypatch,
):
    suffix = uuid.uuid4().hex[:12]
    user_id = f"quota-recovery-user-{suffix}"
    project_id = f"quota-recovery-project-{suffix}"
    session_ids = [f"quota-recovery-session-{suffix}-{index}" for index in range(3)]
    trigger_ids = [f"quota-recovery-message-{suffix}-{index}" for index in range(3)]
    original_run_ids = [f"quota-recovery-run-{suffix}-{index}" for index in range(3)]
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            User(
                id=user_id,
                username=user_id,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            Project(
                id=project_id,
                user_id=user_id,
                name="Recovery quota",
                slug=project_id,
                created_at=now,
                updated_at=now,
            )
        )
        for session_id, trigger_id, run_id in zip(
            session_ids,
            trigger_ids,
            original_run_ids,
            strict=True,
        ):
            db.add(
                Session(
                    id=session_id,
                    user_id=user_id,
                    project_id=project_id,
                    status="error",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                Message(
                    id=trigger_id,
                    session_id=session_id,
                    user_id=user_id,
                    role="user",
                    created_at=now,
                )
            )
            db.add(
                AgentDriverState(
                    session_id=session_id,
                    user_id=user_id,
                    generation=7,
                    run_id=run_id,
                    owner_id="dead-worker",
                    phase="reserved",
                    trigger_message_id=trigger_id,
                    lease_expires_at=now - timedelta(seconds=1),
                    started_at=now - timedelta(minutes=1),
                    updated_at=now - timedelta(seconds=1),
                )
            )

    import core.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: type("Quota", (), {"max_concurrent_agents": 1})(),
    )
    gates = {session_id: asyncio.Event() for session_id in session_ids}
    started: list[str] = []

    async def holding_run_loop(session_id, user_id, *, lease):
        assert user_id == lease.user_id
        started.append(session_id)
        await gates[session_id].wait()
        await lease.release(session_status="idle")

    async def no_outboxes(_records):
        return 0

    async def no_rejoins(_records, **_kwargs):
        return []

    async def no_repairs(_records):
        return []

    async def no_unbound():
        return []

    async def no_subagents():
        return False

    async def no_settle():
        return 0

    async def no_inbox():
        return []

    async def no_effects():
        from agent.effect_ledger import EffectRecoveryResult

        return EffectRecoveryResult()

    from agent import driver as driver_module

    recover_all = driver_module.recover_expired_driver_records

    async def recover_scoped_records():
        return [
            record for record in await recover_all() if record.session_id in session_ids
        ]

    monkeypatch.setattr("agent.loop.run_loop", holding_run_loop)
    monkeypatch.setattr(
        "agent.driver.recover_expired_driver_records",
        recover_scoped_records,
    )
    monkeypatch.setattr(
        "agent.task_handoff.recover_task_handoff_outboxes",
        no_outboxes,
    )
    monkeypatch.setattr(
        "agent.recovery.reconcile_completed_task_handoffs",
        no_rejoins,
    )
    monkeypatch.setattr("agent.recovery.repair_expired_sessions", no_repairs)
    monkeypatch.setattr("agent.recovery.resume_unbound_task_children", no_unbound)
    monkeypatch.setattr("agent.subagent_runtime.has_subagent_state", no_subagents)
    monkeypatch.setattr("agent.inbox.settle_orphaned_claims", no_settle)
    monkeypatch.setattr("agent.inbox.resume_claimable_inbox_sessions", no_inbox)
    monkeypatch.setattr(
        "agent.effect_ledger.recover_external_effects_once",
        no_effects,
    )

    import agent.recovery as recovery_module

    remaining = set(session_ids)
    try:
        for pass_number in range(3):
            result = await service_module.recover_agent_work_once()
            assert result.expired_markers == 3 - pass_number
            assert result.resumed_prompts == 1
            for _ in range(10):
                if len(started) == pass_number + 1:
                    break
                await asyncio.sleep(0)
            assert len(started) == pass_number + 1
            active = started[-1]
            assert active in remaining
            remaining.remove(active)

            for session_id in remaining:
                state = await get_driver_state(session_id)
                index = session_ids.index(session_id)
                assert state is not None
                assert state.run_id == original_run_ids[index]
                assert state.generation == 7
                assert state.phase == "reserved"
                assert state.trigger_message_id == trigger_ids[index]

            gates[active].set()
            tasks = list(recovery_module._resume_tasks)
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)

        assert remaining == set()
        assert not any(
            record.session_id in session_ids
            for record in await recover_scoped_records()
        )
    finally:
        for gate in gates.values():
            gate.set()
        tasks = list(recovery_module._resume_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_subagent_interrupt_and_inbox_scan_run_without_expired_driver(
    monkeypatch,
):
    calls: list[str] = []

    async def records():
        calls.append("drivers")
        return []

    async def has_state():
        calls.append("has")
        return True

    async def interrupts():
        calls.append("interrupts")
        return 1

    async def legacy_outboxes(_records):
        calls.append("legacy_outboxes")
        return 0

    async def new_outboxes(_records):
        calls.append("subagent_outboxes")
        return 0

    async def rejoin(_records, **_kwargs):
        calls.append("rejoin")
        return []

    async def repair(_records):
        calls.append("repair")
        return []

    async def resume(_records):
        calls.append("reserved")
        return [], []

    async def legacy_unbound():
        calls.append("legacy_unbound")
        return []

    async def activation_scan():
        calls.append("activation_scan")
        return ["activation-1"]

    async def settle_main_inbox():
        calls.append("main_inbox_settle")
        return 0

    async def resume_main_inbox():
        calls.append("main_inbox_resume")
        return ["main-session-1"]

    monkeypatch.setattr("agent.driver.recover_expired_driver_records", records)
    monkeypatch.setattr("agent.subagent_runtime.has_subagent_state", has_state)
    monkeypatch.setattr("agent.subagent_runtime.consume_interrupt_requests", interrupts)
    monkeypatch.setattr(
        "agent.task_handoff.recover_task_handoff_outboxes", legacy_outboxes
    )
    monkeypatch.setattr(
        "agent.subagent_runtime.recover_subagent_outboxes", new_outboxes
    )
    monkeypatch.setattr("agent.recovery.reconcile_completed_task_handoffs", rejoin)
    monkeypatch.setattr("agent.recovery.repair_expired_sessions", repair)
    monkeypatch.setattr("agent.recovery.resume_reserved_prompts", resume)
    monkeypatch.setattr("agent.recovery.resume_unbound_task_children", legacy_unbound)
    monkeypatch.setattr(
        "agent.recovery.resume_claimable_subagent_activations", activation_scan
    )
    monkeypatch.setattr("agent.inbox.settle_orphaned_claims", settle_main_inbox)
    monkeypatch.setattr(
        "agent.inbox.resume_claimable_inbox_sessions", resume_main_inbox
    )

    result = await service_module.recover_agent_work_once()
    assert calls.index("interrupts") < calls.index("activation_scan")
    assert calls.index("interrupts") < calls.index("reserved")
    assert result.applied_subagent_interrupts == 1
    assert result.resumed_subagent_activations == 1
    assert result.resumed_inbox_sessions == 1


@pytest.mark.asyncio
async def test_service_stop_quiesces_recovery_owned_children(monkeypatch):
    seen = []

    async def quiesce(*, timeout):
        seen.append(timeout)

    monkeypatch.setattr("agent.recovery.quiesce_recovery_tasks", quiesce)
    service = AgentRecoveryService(stop_timeout_seconds=2.5)
    await service.stop()
    assert seen == [2.5]


@pytest.mark.asyncio
async def test_periodic_loop_retries_after_initial_failure(monkeypatch):
    reached_retry = asyncio.Event()
    calls = 0

    async def recover_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database outage")
        reached_retry.set()
        return AgentRecoveryResult()

    monkeypatch.setattr(service_module, "recover_agent_work_once", recover_once)
    service = AgentRecoveryService(interval_seconds=0.01)

    assert await service.start() is None
    assert service.running is True
    await asyncio.wait_for(reached_retry.wait(), timeout=1)
    await service.stop()

    assert calls >= 2
    assert service.running is False
    # Shutdown remains idempotent.
    await service.stop()


@pytest.mark.asyncio
async def test_stop_cancels_only_after_graceful_timeout(monkeypatch):
    pass_started = asyncio.Event()
    pass_cancelled = asyncio.Event()
    never_finishes = asyncio.Event()
    calls = 0

    async def recover_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            return AgentRecoveryResult()
        pass_started.set()
        try:
            await never_finishes.wait()
        finally:
            pass_cancelled.set()
        return AgentRecoveryResult()

    monkeypatch.setattr(service_module, "recover_agent_work_once", recover_once)
    service = AgentRecoveryService(
        interval_seconds=0.01,
        stop_timeout_seconds=0.02,
    )
    await service.start()
    await asyncio.wait_for(pass_started.wait(), timeout=1)

    await asyncio.wait_for(service.stop(), timeout=1)

    assert pass_cancelled.is_set()
    assert service.running is False


@pytest.mark.asyncio
async def test_periodic_loop_recovers_lease_that_expires_after_startup(monkeypatch):
    suffix = uuid.uuid4().hex[:12]
    user_id = f"periodic-user-{suffix}"
    project_id = f"periodic-project-{suffix}"
    session_id = f"periodic-session-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            User(
                id=user_id,
                username=f"periodic-{suffix}",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            Project(
                id=project_id,
                user_id=user_id,
                name="Periodic recovery",
                slug=f"periodic-{suffix}",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            Session(
                id=session_id,
                user_id=user_id,
                project_id=project_id,
                status="idle",
                created_at=now,
                updated_at=now,
            )
        )

    lease = await reserve_run(session_id, user_id)
    await lease.stop_monitor()
    service = AgentRecoveryService(interval_seconds=0.02)
    periodic_converged = asyncio.Event()
    recover_once = service_module.recover_agent_work_once

    async def observe_target_recovery():
        result = await recover_once()
        state = await get_driver_state(session_id)
        if state is not None and state.phase == "idle" and state.generation == 2:
            periodic_converged.set()
        return result

    monkeypatch.setattr(
        service_module,
        "recover_agent_work_once",
        observe_target_recovery,
    )
    try:
        initial = await service.start()
        assert initial is not None
        assert all(
            item.session_id != session_id for item in await _expired_records_snapshot()
        )
        state = await get_driver_state(session_id)
        assert state is not None
        assert state.run_id == lease.run_id

        # The replacement process was already running when this dead owner's
        # lease crossed its deadline. A later periodic pass must still repair it.
        async with get_db_session() as db:
            await db.execute(
                update(AgentDriverState)
                .where(AgentDriverState.session_id == session_id)
                .values(
                    lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1))
                )
            )

        # Observe the production pass result rather than polling a second
        # AsyncSession against pytest's single-connection in-memory SQLite.
        # The latter is not a multi-connection concurrency model and can make
        # sqlite commit while the recovery SELECT cursor is still active.
        await asyncio.wait_for(periodic_converged.wait(), timeout=1)
        await service.stop()
        state = await get_driver_state(session_id)
        assert state is not None
        assert state.phase == "idle"
        assert state.generation == 2
    finally:
        await service.stop()
        await lease.release(session_status="error")


async def _expired_records_snapshot() -> list[RecoveredDriver]:
    """Use the production snapshot and immediately return its current markers."""
    from agent.driver import recover_expired_driver_records

    return await recover_expired_driver_records()
