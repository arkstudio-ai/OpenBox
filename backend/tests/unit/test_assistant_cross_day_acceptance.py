"""Two projects survive a 49-hour application-clock jump and SQL reopen.

HTTP, dispatch, processor, source validation and recovery are real. Only the
external provider is deterministic. The clock jump is confined to this test
process; it does not change database/server clocks or claim elapsed browser time.
"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

from sqlalchemy import select

from agent import processor
from agent.recovery_service import AgentRecoveryService
from assistant.reporting import REPORT_TOOLS
from assistant.results import deliver_task_result
from db.base import close_engine, get_db_session, init_engine
from db.models.assistant import TaskResult, TaskSubmission
from db.models.message import Message
from db.models.project import Project
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_assistant_api import client_for, signing_key  # noqa: F401
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_report_binding_acceptance import (
    counts, read_call, run, setup,
)
from tests.unit.test_assistant_report_recovery_acceptance import _execution_facts


def advance_application_clock(monkeypatch, delta):
    """Advance loaded application datetime aliases and epoch TTLs, not timers."""
    backend = Path(__file__).resolve().parents[2]
    original_time = time.time

    class ClockMeta(type):
        def __instancecheck__(cls, value):
            # Existing ORM timestamps remain ordinary datetime objects.
            return isinstance(value, datetime)

    class LaterDatetime(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + delta

        @classmethod
        def utcnow(cls):
            return datetime.utcnow() + delta

        @classmethod
        def today(cls):
            return cls.now()

    patched = []
    # All dispatch and recovery modules have already run before this jump.
    # Limit replacement to application code, leaving test/third-party classes
    # and asyncio's monotonic scheduling untouched.
    for name, module in list(sys.modules.items()):
        location = getattr(module, "__file__", None)
        if not location or getattr(module, "datetime", None) is not datetime:
            continue
        try:
            relative = Path(location).resolve().relative_to(backend)
        except ValueError:
            continue
        if relative.parts[0] in {"tests", ".venv"}:
            continue
        monkeypatch.setattr(module, "datetime", LaterDatetime)
        patched.append(name)
    assert {"assistant.commands", "agent.driver", "agent.inbox"} <= set(patched)
    monkeypatch.setattr(time, "time", lambda: original_time() + delta.total_seconds())
    return sorted(patched)


async def result_rows(result_ids):
    async with get_db_session() as db:
        rows = list((await db.scalars(select(TaskResult).where(
            TaskResult.id.in_(result_ids)).order_by(TaskResult.id))).all())
        return json.loads(json.dumps([
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in rows
        ], default=str))


async def latest_result(task_id):
    async with get_db_session() as db:
        return await db.scalar(select(TaskResult).where(TaskResult.task_id == task_id)
                               .order_by(TaskResult.created_at.desc(), TaskResult.id.desc()))


async def test_reverse_project_results_restore_after_49_hours_and_continue_original_session(
    monkeypatch, record_property,
):
    owner, workspace, main = await setup(monkeypatch)
    suffix = uuid4().hex[:12]
    projects = {key: f"cross-day-{key}-{suffix}" for key in ("A", "B")}
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add_all([Project(id=value, user_id=owner, workspace_id=workspace,
                           name=f"Cross day {key}", created_at=now, updated_at=now)
                    for key, value in projects.items()])
        url = db.get_bind().url.render_as_string(hide_password=False)

    prompts = {"A": "PROJECT_A_ORIGINAL: calculate 13 + 17 using text only.",
               "B": "PROJECT_B_ORIGINAL: calculate 21 + 22 using text only."}
    followup_text = "PROJECT_A_FOLLOWUP: add 11 to the original answer, retaining the old result."
    bodies = {"A": "PROJECT_A_RESULT: 13 + 17 = 30.",
              "B": "PROJECT_B_RESULT: 21 + 22 = 43.",
              "A2": "PROJECT_A_REVISED_RESULT: original 30 + 11 = 41."}
    accepted, execution_calls, report_calls, results = {}, [], {}, {}
    active_report = None

    async def stream(**kwargs):
        ctx = kwargs["ctx"]
        payload = json.dumps(kwargs["messages"])
        if ctx.session_id != main.id:
            key = next(key for key, receipt in accepted.items()
                       if receipt["execution_session_id"] == ctx.session_id)
            previous = sum(call["project"] == key for call in execution_calls)
            assert prompts[key] in payload
            assert prompts["B" if key == "A" else "A"] not in payload
            phase = "A2" if key == "A" and previous else key
            if phase == "A2":
                assert bodies["A"] in payload and followup_text in payload
            execution_calls.append({"project": key, "phase": phase, "session_id": ctx.session_id,
                                    "run_id": ctx.run_id, "generation": ctx.run_generation})
            yield {"type": "text_delta", "text": bodies[phase]}
        else:
            assert active_report is not None and ctx.sandbox is None
            assert {tool.id for tool in kwargs["tools"].values()} == REPORT_TOOLS
            calls = report_calls.setdefault(active_report, [])
            calls.append(ctx.message_id)
            if len(calls) == 1:
                yield read_call(kwargs, results[active_report].id, f"read-cross-day-{active_report}")
                yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                return
            assert bodies[active_report] in payload
            assert results[active_report].result_message_id in payload
            yield {"type": "text_delta", "text": f"The original task reports: {bodies[active_report]}"}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    old_service = AgentRecoveryService(interval_seconds=3600)
    initial = await old_service.start()
    assert initial is not None and initial.resumed_inbox_sessions == 0
    try:
        async with client_for(owner, workspace, monkeypatch) as client:
            empty = (await client.get("/api/assistant")).json()
            for key in ("A", "B"):
                response = await client.post("/api/assistant/tasks", json={
                    "idempotency_key": f"create-project-{key}", "project_id": projects[key],
                    # Same titles must never be used as continuation identity.
                    "title": "Same titled report", "input": {"text": prompts[key], "delivery": "followup"},
                })
                assert response.status_code == 202, response.text
                accepted[key] = response.json()
            assert accepted["A"]["execution_session_id"] != accepted["B"]["execution_session_id"]
            # B finishes and is reported first although A was accepted first.
            for key in ("B", "A"):
                await run(accepted[key]["execution_session_id"], owner)
                results[key] = await latest_result(accepted[key]["task_id"])
                assert results[key] is not None and results[key].outcome == "succeeded"
                await deliver_task_result(results[key].id)
                active_report = key
                await run(main.id, owner)
            await old_service.run_once()
            before = (await client.get("/api/assistant")).json()
    finally:
        await old_service.stop()
    assert before["unread_count"] == 2 and all(row["available"] for row in before["answers"])
    old_results = await result_rows([results["A"].id, results["B"].id])
    assert all(row["delivery_state"] == "processed" for row in old_results)
    old_facts = {key: await _execution_facts(receipt["task_id"]) for key, receipt in accepted.items()}
    old_counts = await counts(owner, main.id)
    assert old_counts == {"commands": 2, "tasks": 2, "execution_sessions": 2, "submissions": 2,
                          "execution_inputs": 2, "results": 2, "report_inputs": 2, "processed_events": 2}
    await close_engine()
    clock_modules = advance_application_clock(monkeypatch, timedelta(hours=49))
    init_engine(url)
    recovered_service = AgentRecoveryService(interval_seconds=3600)
    try:
        recovered = await recovered_service.start()
        assert recovered is not None
        assert recovered.resumed_inbox_sessions == recovered.assistant_results_recovered == 0
        async with client_for(owner, workspace, monkeypatch) as client:
            restored = (await client.get("/api/assistant")).json()
            assert restored["session"]["id"] == main.id
            assert restored["unread_count"] == 2
            assert restored["high_water_mark"] == before["high_water_mark"]
            rows = {row["task"]["id"]: row for row in restored["tasks"]}
            for key, receipt in accepted.items():
                row = rows[receipt["task_id"]]
                assert row["task"]["project_id"] == projects[key]
                assert row["task"]["title"] == "Same titled report"
                assert row["execution_session"]["id"] == receipt["execution_session_id"]
                assert row["latest_result"]["result_id"] == results[key].id
            # The old display receipt has expired, but reloading yields a new
            # verifiable receipt. Neither rejection nor marking read wakes work.
            old_display = before["answers"][0]
            rejected = await client.post("/api/assistant/read-cursor", json={
                "last_seen_sequence": old_display["sequence"], "display_token": old_display["display_token"]})
            assert rejected.status_code == 409
            fresh = restored["answers"][0]
            marked = await client.post("/api/assistant/read-cursor", json={
                "last_seen_sequence": fresh["sequence"], "display_token": fresh["display_token"]})
            assert marked.status_code == 200
            assert (await client.get("/api/assistant")).json()["unread_count"] == 0
            cursor, public_events = empty["event_cursor"], []
            for _ in range(20):
                response = await client.get("/api/assistant/events", params={"after": cursor, "limit": 200})
                assert response.status_code == 200
                page = response.json()
                assert page["state"] == "ready"
                public_events.extend(page["events"])
                cursor = page["next_cursor"]
                if not page["has_more"]:
                    break
            else:
                raise AssertionError("bounded event replay failed to converge")
            assert page["next_sequence"] == restored["high_water_mark"]
            processed = [event["result_id"] for event in public_events
                         if event["kind"] == "assistant.result.processed"]
            assert processed == [results["B"].id, results["A"].id]
            assert await counts(owner, main.id) == old_counts
            assert {key: await _execution_facts(receipt["task_id"])
                    for key, receipt in accepted.items()} == old_facts
            assert await result_rows([results["A"].id, results["B"].id]) == old_results
            assert [call["phase"] for call in execution_calls] == ["B", "A"]
            a = rows[accepted["A"]["task_id"]]
            command = {"idempotency_key": "day-three-edit-A", "action": "input",
                       "expected_revision": a["task"]["control_revision"],
                       "input": {"text": followup_text, "delivery": "followup"}}
            endpoint = f"/api/assistant/tasks/{accepted['A']['task_id']}/commands"
            response = await client.post(endpoint, json=command)
            assert response.status_code == 202, response.text
            continued = response.json()
            assert continued["execution_session_id"] == accepted["A"]["execution_session_id"]
            assert (await client.post(endpoint, json=command)).json() == continued
            await run(continued["execution_session_id"], owner)
            results["A2"] = await latest_result(accepted["A"]["task_id"])
            assert results["A2"].id != results["A"].id and results["A2"].outcome == "succeeded"
            await deliver_task_result(results["A2"].id)
            active_report = "A2"
            await run(main.id, owner)
            await recovered_service.run_once()
            final = (await client.get("/api/assistant")).json()
    finally:
        await recovered_service.stop()

    assert [call["phase"] for call in execution_calls] == ["B", "A", "A2"]
    assert execution_calls[1]["session_id"] == execution_calls[2]["session_id"]
    assert execution_calls[1]["run_id"] != execution_calls[2]["run_id"]
    assert execution_calls[2]["generation"] > execution_calls[1]["generation"]
    assert await _execution_facts(accepted["B"]["task_id"]) == old_facts["B"]
    assert await result_rows([results["A"].id, results["B"].id]) == old_results
    measured = await counts(owner, main.id)
    assert measured == {"commands": 3, "tasks": 2, "execution_sessions": 2, "submissions": 3,
                        "execution_inputs": 3, "results": 3, "report_inputs": 3, "processed_events": 3}
    assert final["unread_count"] == 1 and all(row["available"] for row in final["answers"])
    async with get_db_session() as db:
        first = await db.get(TaskSubmission, accepted["A"]["submission_id"])
        last = await db.get(TaskSubmission, continued["submission_id"])
        # Accepted-at comes from the runtime application clock. TaskResult's
        # server-default creation timestamp deliberately uses the unchanged DB
        # clock, so it is not evidence of this process-local time transition.
        assert last.accepted_at - first.accepted_at >= timedelta(hours=49)
        persisted_clock = {"first_submission": first.accepted_at.isoformat(),
                           "continued_submission": last.accepted_at.isoformat()}
        for result in results.values():
            row = await db.get(TaskResult, result.id)
            assert row.delivery_state == "processed"
            assert (await db.get(Message, row.processed_message_id)).finish == "stop"
    for session_id in (main.id, *(receipt["execution_session_id"] for receipt in accepted.values())):
        assert (await verify_agent_event_parity(session_id, user_id=owner)).ok
    record_property("assistant_acceptance", json.dumps({
        "scenario": "PA-05 and PA-31 cross-day server acceptance",
        "scope": "real HTTP/SQL/loop/processor/recovery with deterministic external provider",
        "clock": {"advanced_hours": 49, "patched_application_modules": clock_modules,
                  "database_server_clock_unchanged": True, "monotonic_clock_unchanged": True,
                  "persisted_acceptance": persisted_clock},
        "database_reopened": True, "recovery": asdict(recovered), "projects": projects,
        "creation_receipts": accepted, "continuation_receipt": continued,
        "execution_calls": execution_calls, "completion_order": processed,
        "result_ids": {key: row.id for key, row in results.items()},
        "old_results_unchanged": True, "other_project_execution_unchanged": old_facts["B"],
        "counts": measured, "expired_display_rejected": True, "read_and_recovery_did_not_run": True,
    }))
