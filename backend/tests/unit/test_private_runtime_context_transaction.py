"""Ready actor preparation uses one current SQL authorization, never a cache.

Reuse the real Task/Inbox/Driver and Action Server journal fixture. Only its
guest OS boundary is replaced; no QA, cloud SDK, Docker or provider is used.
"""
from dataclasses import replace

from sqlalchemy import select, text

import pytest

from assistant import scheduling
from assistant.policy import AssistantError
from agent.driver import LeaseLostError
from db.base import get_db_session, get_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantTask
from db.models.private_runtime import PrivateRuntimeBinding
from db.models.resource_control import ResourceControlLease
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from sandbox import runtime_operation as runtime
from tests.offline_wuying import install_wuying_offline_guard
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_wuying_browser_mount import actor_runtime, mounted, server  # noqa: F401


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    install_wuying_offline_guard(monkeypatch)


async def origins(fixture):
    async with get_db_session() as db:
        return list((await db.scalars(select(AgentEvent).where(
            AgentEvent.session_id == fixture.lease.session_id,
            AgentEvent.kind == "resource.runtime_requested"))).all())


async def test_ready_context_checks_sources_once_each_call_and_keeps_original_event(actor_runtime, monkeypatch):
    fixture = actor_runtime
    await runtime.ensure_private_runtime_control(fixture.client, fixture.lease)
    assert await origins(fixture) == []
    checked = []
    real = scheduling.require_runnable_locked
    from session import agent_event_log
    real_append = agent_event_log.append_agent_event_locked

    async def observe(db, session, **kwargs):
        checked.append((db, db.get_transaction()))
        return await real(db, session, **kwargs)

    async def same_transaction(db, session, **kwargs):
        if kwargs.get("kind") == "resource.runtime_requested":
            assert (db, db.get_transaction()) == checked[-1]
        return await real_append(db, session, **kwargs)

    async def no_remote(*args, **kwargs):
        pytest.fail("An already bound runtime must not probe or bind again")

    monkeypatch.setattr(scheduling, "require_runnable_locked", observe)
    monkeypatch.setattr(agent_event_log, "append_agent_event_locked", same_transaction)
    monkeypatch.setattr(fixture.client, "resource_status", no_remote)
    monkeypatch.setattr(fixture.client, "resource_command", no_remote)
    first = await runtime.runtime_context(fixture.client, fixture.lease)
    assert len(checked) == 1
    second = await runtime.runtime_context(fixture.client, fixture.lease)
    assert len(checked) == 2 and checked[0][0] is not checked[1][0]
    assert first == second
    events = await origins(fixture)
    assert len(events) == 1 and events[0].id == first[-1]
    assert events[0].payload["resource_context"]["journal_id"] == first[2]


@pytest.mark.parametrize("change", ["replace", "remove"])
@pytest.mark.parametrize("boundary", ["source", "final_validation"])
async def test_ready_context_refuses_client_route_changed_during_authorization(
        actor_runtime, monkeypatch, change, boundary):
    fixture = actor_runtime
    await runtime.ensure_private_runtime_control(fixture.client, fixture.lease)
    real = scheduling.require_runnable_locked
    real_validate = runtime.controls.validate_locked
    validations = 0

    def change_route():
        fixture.client.private_runtime_route = (
            replace(fixture.route, revision=fixture.route.revision + 1) if change == "replace" else None)

    async def change_after_source_check(db, session, **kwargs):
        await real(db, session, **kwargs)
        if boundary == "source":
            change_route()

    async def change_after_final_validation(*args, **kwargs):
        nonlocal validations
        row = await real_validate(*args, **kwargs)
        validations += 1
        if boundary == "final_validation" and validations == 2:
            change_route()
        return row

    async def no_remote(*args, **kwargs):
        pytest.fail("A changed client route must not probe or bind its replacement")

    monkeypatch.setattr(scheduling, "require_runnable_locked", change_after_source_check)
    monkeypatch.setattr(runtime.controls, "validate_locked", change_after_final_validation)
    monkeypatch.setattr(fixture.client, "resource_status", no_remote)
    monkeypatch.setattr(fixture.client, "resource_command", no_remote)
    with pytest.raises(AssistantError):
        await runtime.runtime_context(fixture.client, fixture.lease)
    events = await origins(fixture)
    # The final check runs after the original origin transaction commits.
    # It may retain that original fact, but must not return usable context.
    assert len(events) == (0 if boundary == "source" else 1)
    if events:
        assert events[0].payload["resource_context"]["fence"]["epoch"] == 1


@pytest.mark.parametrize("change", ["pause", "membership", "generation", "binding_revision",
                                    "binding_blocked", "epoch", "journal"])
async def test_ready_context_rejects_current_sql_changes_without_remote_or_new_origin(
        actor_runtime, monkeypatch, change):
    fixture = actor_runtime
    first = await runtime.runtime_context(fixture.client, fixture.lease)
    before, = await origins(fixture)
    async with get_db_session() as db:
        if change == "pause":
            task = await db.get(AssistantTask, fixture.args["task_id"])
            task.desired_state = "paused"
            task.control_revision += 1
        elif change == "membership":
            member = await db.get(WorkspaceMember, (fixture.args["workspace_id"], fixture.lease.user_id))
            member.status = "removed"
        elif change == "generation":
            driver = await db.get(AgentDriverState, fixture.lease.session_id)
            driver.generation += 1
        elif change.startswith("binding_"):
            binding = await db.get(PrivateRuntimeBinding, fixture.route.binding_id)
            if change == "binding_revision":
                binding.revision += 1
            else:
                binding.status = "blocked"
        else:
            resource = await db.get(ResourceControlLease, first[1].resource_id)
            if change == "epoch":
                resource.epoch += 1
            else:
                # Even a self-consistent replacement journal cannot replace
                # the run's persisted resource origin.
                resource.remote_journal_id = "f" * 32
                resource.remote_status = {**resource.remote_status, "journal_id": "f" * 32}

    async def no_remote(*args, **kwargs):
        pytest.fail("A revoked/replaced ready binding must not trigger guest IO")

    monkeypatch.setattr(fixture.client, "resource_status", no_remote)
    monkeypatch.setattr(fixture.client, "resource_command", no_remote)
    with pytest.raises((AssistantError, LeaseLostError)):
        await runtime.runtime_context(fixture.client, fixture.lease)
    after, = await origins(fixture)
    assert (after.id, after.payload) == (before.id, before.payload)


async def test_cold_network_boundaries_see_committed_identity_and_unlocked_driver(actor_runtime, monkeypatch):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent PostgreSQL row-lock proof")
    fixture = actor_runtime
    checkpoints = []
    status, command = fixture.client.resource_status, fixture.client.resource_command

    async def inspect_committed(stage):
        # These are independent connections, not the caller's ORM Session.
        # NOWAIT makes a held caller lock an immediate, meaningful failure.
        async with get_db_session() as db:
            await db.execute(text("SET LOCAL lock_timeout = '500ms'"))
            for model, column in ((Session, Session.id), (AgentDriverState, AgentDriverState.session_id)):
                assert await db.scalar(select(column).where(column == fixture.lease.session_id)
                    .with_for_update(nowait=True)) is not None
            assert await db.scalar(select(PrivateRuntimeBinding.id).where(
                PrivateRuntimeBinding.id == fixture.route.binding_id).with_for_update(nowait=True))
            row = await db.scalar(select(ResourceControlLease).where(
                ResourceControlLease.workspace_id == fixture.args["workspace_id"]).with_for_update(nowait=True))
            assert row is not None and row.remote_status is None
            assert (row.remote_journal_id is None) == (stage == "status")
            assert await db.scalar(select(AgentEvent.id).where(
                AgentEvent.session_id == fixture.lease.session_id,
                AgentEvent.kind == "resource.runtime_requested")) is None
            checkpoints.append((stage, row.id, row.remote_journal_id))

    async def read_status():
        await inspect_committed("status")
        return await status()

    async def bind(action, payload):
        await inspect_committed("bind")
        assert action == "bind" and payload["journal_id"] == checkpoints[-1][2]
        return await command(action, payload)

    monkeypatch.setattr(fixture.client, "resource_status", read_status)
    monkeypatch.setattr(fixture.client, "resource_command", bind)
    context = await runtime.runtime_context(fixture.client, fixture.lease)
    assert checkpoints == [("status", context[1].resource_id, None),
                           ("bind", context[1].resource_id, context[2])]
    assert len(await origins(fixture)) == 1
