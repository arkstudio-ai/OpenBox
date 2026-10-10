"""Independent HTTP clients serialize controls and replay durable receipts."""
import asyncio
from contextvars import ContextVar
import json

import pytest
from sqlalchemy import func, select, text

from assistant import control as controls
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult, TaskSubmission
from db.models.message import Message
from tests.unit.test_assistant_api import client_for
from tests.unit.test_assistant_control import paused
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_steering import running, terminal, view


async def counts(task_id, session_id):
    async with get_db_session() as db:
        return {name: await db.scalar(select(func.count()).select_from(model).where(predicate))
            for name, model, predicate in (
                ("commands", AssistantCommand, AssistantCommand.target_id == task_id),
                ("submissions", TaskSubmission, TaskSubmission.task_id == task_id),
                ("inputs", AgentInboxItem, AgentInboxItem.session_id == session_id),
                ("messages", Message, Message.session_id == session_id),
                ("results", TaskResult, TaskResult.task_id == task_id),
                ("control_events", AgentEvent, (AgentEvent.session_id == session_id)
                    & (AgentEvent.kind == "assistant.control.accepted")),
            )}


@pytest.mark.parametrize("winner,competitor", [("pause", "resume"), ("resume", "pause"), ("pause", "cancel")])
async def test_independent_http_controls_commit_once_and_replay_after_reopen(
        monkeypatch, record_property, winner, competitor):
    if winner == "resume":
        args, created, lease, _, _, current = await paused(monkeypatch)
        batch = None
    else:
        args, created, lease, batch = await running()
        current = await view(args)
    task_id, session_id = created["task_id"], created["execution_session_id"]
    revision = current["task"]["control_revision"]
    binding = current["run_binding"]
    expected_run = ({"run_id": binding["run_id"], "generation": binding["generation"]}
                    if binding and binding["phase"] != "idle" else None)
    before = await counts(task_id, session_id)
    async with get_db_session() as db:
        url = db.get_bind().url.render_as_string(hide_password=False)
        dialect = db.get_bind().dialect.name
    # The API's post-commit scanner is deliberately deferred. It must not add
    # a second state transition while two HTTP acknowledgements are compared.
    async def deferred_recovery(**kwargs):
        return 0, []
    monkeypatch.setattr(controls, "recover_controls", deferred_recovery)
    original_lock = controls.lock_actor
    device = ContextVar("control_acceptance_device")
    both_arrived, winner_locked = asyncio.Event(), asyncio.Event()
    connections = {}

    async def contended_actor_lock(db, actor_id):
        label = device.get()
        connections[label] = {"backend_pid": await db.scalar(text("SELECT pg_backend_pid()")),
                              "transaction_id": await db.scalar(text("SELECT txid_current()"))}
        if len(connections) == 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), timeout=10)
        if label == "second":
            await asyncio.wait_for(winner_locked.wait(), timeout=10)
        await original_lock(db, actor_id)
        if label == "first":
            winner_locked.set()

    bodies = [{"idempotency_key": "device-first", "action": winner,
               "expected_revision": revision, "expected_run": expected_run},
              {"idempotency_key": "device-second", "action": competitor,
               "expected_revision": revision, "expected_run": expected_run}]
    endpoint = f"/api/assistant/tasks/{task_id}/commands"

    async def send(client, index):
        token = device.set("first" if index == 0 else "second")
        try:
            return await client.post(endpoint, json=bodies[index])
        finally:
            device.reset(token)

    try:
        async with client_for(args["user_id"], args["workspace_id"], monkeypatch) as first, \
                client_for(args["user_id"], args["workspace_id"], monkeypatch) as second:
            if dialect == "postgresql":
                with monkeypatch.context() as race_patch:
                    race_patch.setattr(controls, "lock_actor", contended_actor_lock)
                    accepted, rejected = await asyncio.gather(send(first, 0), send(second, 1))
                assert len({row["backend_pid"] for row in connections.values()}) == 2
                assert len({row["transaction_id"] for row in connections.values()}) == 2
            else:
                # SQLite checks the same HTTP/replay contract, never claims
                # to be evidence for independent PostgreSQL lock contention.
                accepted, rejected = await send(first, 0), await send(second, 1)
            assert accepted.status_code == 202, accepted.text
            assert rejected.status_code == 409, rejected.text
            receipt = accepted.json()
            assert receipt["action"] == winner and receipt["expected_run"] == expected_run
            assert receipt["task_revision"] > revision
            latest = (await second.get(f"/api/assistant/tasks/{task_id}")).json()
            conflict = rejected.json()["detail"]
            assert conflict["code"] == "ASSISTANT_REVISION_CONFLICT"
            assert conflict["current_task"] == {key: latest["task"][key]
                for key in ("id", "desired_state", "observed_state", "control_revision")}
            assert latest["latest_control"]["command_id"] == receipt["command_id"]
        after = await counts(task_id, session_id)
        assert after == {**before, "commands": before["commands"] + 1,
                        "control_events": before["control_events"] + 1}
        # Lose the first acknowledgement and every connection. A new client
        # still gets the original receipt despite its now-stale revision.
        await close_engine()
        init_engine(url)
        async with client_for(args["user_id"], args["workspace_id"], monkeypatch) as reconnected:
            replay = await reconnected.post(endpoint, json=bodies[0])
            assert replay.status_code == 202 and replay.json() == receipt
            changed = await reconnected.post(endpoint, json={**bodies[0], "action": competitor})
            assert changed.status_code == 409
            assert changed.json()["detail"]["code"] == "ASSISTANT_COMMAND_CONFLICT"
            stored = await reconnected.get(f"/api/assistant/commands/{receipt['command_id']}")
            assert stored.status_code == 200 and stored.json()["receipt"] == receipt
        assert await counts(task_id, session_id) == after
        async with get_db_session() as db:
            task = await db.get(AssistantTask, task_id)
            driver = await db.get(AgentDriverState, session_id)
            assert task.control_revision == receipt["task_revision"]
            assert driver.generation == lease.generation
            assert not await db.scalar(select(AssistantCommand.id).where(
                AssistantCommand.target_id == task_id, AssistantCommand.idempotency_key == "device-second"))
            witness = {"task_id": task_id, "execution_session_id": session_id,
                "run_id": lease.run_id, "generation": lease.generation, "dialect": dialect,
                "independent_connections": connections, "submitted_revision": revision,
                "accepted_receipt": receipt, "conflict": conflict, "counts_before": before,
                "counts_after": after, "reopened_receipt_equal": True}
        record_property("assistant_acceptance", json.dumps(witness))
    finally:
        if batch is not None:
            await terminal(lease, batch.messages[0].id)
        await lease.release(session_status="idle")
