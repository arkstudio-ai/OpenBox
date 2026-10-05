"""Real command/result history shares only original reads within one validation."""
from copy import deepcopy
import json
from time import perf_counter
from types import SimpleNamespace

import pytest

from assistant.command_sources import _CommandWalk
from assistant.results import validate_result_source
from db.base import get_db_session
from db.models.assistant import TaskResult
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_command_sources import arguments, no_task_dispatch, settle_execution  # noqa: F401
from tests.unit.test_assistant_context_sources import finish, next_turn
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import call_tool, read_turn
from tests.unit.test_assistant_source_join_queries import sql_reads


@pytest.fixture
async def repeated_result():
    ctx, lease, answer, accepted, report = await read_turn()
    try:
        # Each real admitted command consumes the old answer and the preceding
        # execution. Their frozen derivations overlap but retain distinct IDs.
        for index in range(4):
            output, _, _ = await call_tool(ctx, "history.read", {
                "session_id": accepted["execution_session_id"], "message_ids": [report.id]})
            assert not output.metadata.get("error"), output.output
            await consume_context(ctx)
            output, _, _ = await call_tool(ctx, "tasks.submit", arguments(ctx, answer))
            assert not output.metadata.get("error"), output.output
            accepted = json.loads(output.output)
            await finish(ctx, lease, answer, f"Accepted derived text task {index}.")
            result = await settle_execution(ctx, accepted)
            report = SimpleNamespace(id=result.result_message_id)
            if index < 3:
                ctx, lease, answer = await next_turn(ctx, "Read that result and create another text-only task.")
        return dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id), result.id
    finally:
        await lease.release(session_status="idle")


async def test_real_result_reuses_original_sql_but_preserves_every_output_byte(monkeypatch, repeated_result, record_property):
    scope, result_id = repeated_result

    async def read():
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            started = perf_counter()
            with sql_reads() as queries:
                task, parts = await validate_result_source(db, result, **scope)
            elapsed = perf_counter() - started
            value = {"task": task.id, "parts": [(deepcopy(ref), part.id, deepcopy(part.data)) for ref, part in parts]}
            groups = {"commands": sum("FROM assistant_commands JOIN assistant_task_submissions" in q for q in queries),
                      "schedules": sum("FROM cron_runs" in q for q in queries)}
            return value, {"sql": len(queries), "elapsed_seconds": elapsed, **groups}

    async def uncached(self, db, kind, scope, payload, validate, **kwargs):
        return await validate()

    # Disable this increment only; existing completed-command proofs remain on.
    with monkeypatch.context() as patch:
        patch.setattr(_CommandWalk, "original", uncached)
        baseline, before = await read()
    optimized, after = await read()
    repeated, next_call = await read()
    assert optimized == baseline == repeated
    assert after["sql"] < before["sql"] * 0.75, (before, after)
    assert after["commands"] < before["commands"] and after["schedules"] < before["schedules"]
    assert next_call["sql"] == after["sql"]  # Nothing survives a top-level call.
    measurement = {"baseline": before, "optimized": after, "next_call": next_call}
    record_property("source_walk_measurement", json.dumps(measurement))
    print(json.dumps(measurement))
