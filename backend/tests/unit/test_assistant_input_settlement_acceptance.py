"""Three inputs keep their logical result identity across maintenance recovery."""
from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import func, select

from agent import inbox
from assistant.commands import accept_task_command
from assistant.results import deliver_task_result
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult, TaskSubmission
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_steering import running, terminal


async def test_three_claimed_inputs_settle_once_after_connection_loss_and_maintenance(record_property):
    args, created, lease, initial = await running()
    first = await accept_task_command(**args)
    second = await accept_task_command(**{**args, "idempotency_key": "third-input",
        "expected_revision": first["task_revision"], "prompt": "Keep the original citations"})
    additions = await inbox.claim_inbox_boundary(lease, step=2, include_next_turn=False)
    input_ids = {created["inbox_id"], first["inbox_id"], second["inbox_id"]}
    assert {row.id for row in initial.receipts + additions.receipts} == input_ids
    assert all((row.run_id, row.generation) == (lease.run_id, lease.generation)
               for row in initial.receipts + additions.receipts)
    # The provider's terminal answer committed, but the process stopped before
    # settling any of its three claimed inputs or creating the Result outbox.
    answer = await terminal(lease, additions.messages[-1].id, settle=False)
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        url = db.get_bind().url.render_as_string(hide_password=False)
        assert await db.scalar(select(func.count()).select_from(TaskResult).where(
            TaskResult.task_id == created["task_id"])) == 0
        rows = (await db.scalars(select(AgentInboxItem).where(AgentInboxItem.id.in_(input_ids)))).all()
        assert len(rows) == 3 and all(row.state == "claimed" for row in rows)
        for row in rows:
            row.claim_expires_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    await close_engine()
    init_engine(url)
    settled = await inbox.settle_orphaned_claims()
    assert settled >= 1
    assert await inbox.settle_orphaned_claims() == 0
    async with get_db_session() as db:
        results = (await db.scalars(select(TaskResult).where(TaskResult.task_id == created["task_id"]))).all()
        assert len(results) == 1
        result = results[0]
        assert (result.run_id, result.generation) == (lease.run_id, lease.generation)
        assert result.settlement_fence["run_id"] != lease.run_id
        assert result.settlement_fence["generation"] > lease.generation
        assert result.result_message_id == answer.id and result.outcome == "succeeded"
        assert set(result.consumed_inbox_ids) == input_ids
        assert result.observed_intent_revision == 3 and result.report_attempt == 1
        assert result.delivery_state == "pending" and result.assistant_inbox_id is None
        result_id, settlement_fence = result.id, dict(result.settlement_fence)
        submissions = (await db.scalars(select(TaskSubmission).where(
            TaskSubmission.task_id == created["task_id"]))).all()
        assert len(submissions) == 3 and {row.inbox_id for row in submissions} == input_ids
        assert all(row.source_message_id and row.disposition == "applied" for row in submissions)
        claims = (await db.scalars(select(AgentEvent).where(
            AgentEvent.session_id == lease.session_id, AgentEvent.kind == "inbox.claimed"))).all()
        original_claims = {row.payload["item_id"]: row for row in claims
                           if row.run_id == lease.run_id and row.generation == lease.generation}
        recovered_claims = {row.payload["item_id"]: row for row in claims
                            if row.run_id == settlement_fence["run_id"]}
        assert original_claims.keys() == recovered_claims.keys() == input_ids
        assert all(row.payload["recovered_from"] == {"run_id": lease.run_id, "generation": lease.generation}
                   and row.generation == settlement_fence["generation"] for row in recovered_claims.values())
        assert all(original_claims[row.inbox_id].message_id == row.source_message_id for row in submissions)
        bindings = [{"submission_id": row.id, "inbox_id": row.inbox_id,
                     "message_id": row.source_message_id, "run_id": original_claims[row.inbox_id].run_id,
                     "generation": original_claims[row.inbox_id].generation,
                     "original_claim_event": original_claims[row.inbox_id].id,
                     "recovered_claim_event": recovered_claims[row.inbox_id].id} for row in submissions]
        driver = await db.get(AgentDriverState, lease.session_id)
        assert driver.phase == "idle"
        recovered_generation = driver.generation
    receipt = await deliver_task_result(result_id)
    assert receipt["report_attempt"] == 1
    await close_engine()
    init_engine(url)
    assert await inbox.settle_orphaned_claims() == 0
    assert await deliver_task_result(result_id) == receipt
    async with get_db_session() as db:
        quantities = {name: await db.scalar(select(func.count()).select_from(model).where(predicate))
            for name, model, predicate in (
                ("results", TaskResult, TaskResult.task_id == created["task_id"]),
                ("settled_inputs", AgentInboxItem, AgentInboxItem.id.in_(input_ids)
                    & (AgentInboxItem.state == "settled")),
                ("completion_events", AgentEvent, (AgentEvent.session_id == lease.session_id)
                    & (AgentEvent.kind == "assistant.execution.completed")),
                ("report_inputs", AgentInboxItem, (AgentInboxItem.session_id == args["main_id"])
                    & (AgentInboxItem.origin == "task_result")),
            )}
        assert quantities == {"results": 1, "settled_inputs": 3, "completion_events": 1, "report_inputs": 1}
        assert (await db.get(AgentDriverState, lease.session_id)).generation == recovered_generation
        assert all(row.result_message_id == answer.id for row in
            (await db.scalars(select(AgentInboxItem).where(AgentInboxItem.id.in_(input_ids)))).all())
    assert (await verify_agent_event_parity(lease.session_id, user_id=lease.user_id)).ok
    record_property("assistant_acceptance", json.dumps({"task_id": created["task_id"],
        "execution_session_id": lease.session_id, "run_id": lease.run_id, "generation": lease.generation,
        "observed_intent_revision": 3, "source_bindings": bindings, "result_id": result_id,
        "result_message_id": answer.id, "settlement_fence": settlement_fence,
        "recovery_generation": recovered_generation, "receipt": receipt, "counts": quantities}))
