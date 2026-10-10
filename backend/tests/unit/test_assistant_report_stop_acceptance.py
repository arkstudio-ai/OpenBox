"""A user's report stop survives recovery; explicit retry never reruns execution."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import func, select, text

from agent import inbox, loop, processor
from agent.driver import reserve_run
from agent.recovery_service import AgentRecoveryService
from api import assistant as api, sessions as session_api
from assistant.reporting import REPORT_TOOLS
from assistant.results import deliver_task_result
from core.config import get_config
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, TaskResult
from db.models.message import Message
from session import abort as abort_service
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_report_recovery_acceptance import _execution_facts
from tool.assistant_tools import assistant_tools


@pytest.mark.parametrize("scenario", ["queued_stop", "running_stop", "retry_exhaustion"])
async def test_http_report_stop_and_manual_retry_survive_recovery(monkeypatch, record_property, scenario):
    monkeypatch.setattr(get_config(), "jwt_secret", "report-stop-acceptance-only")
    config = _loop_config()
    config.permission = {"*": "allow"}
    real_history = loop._resolve_history_tool_names
    real_todos = abort_service.settle_running_todos
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    monkeypatch.setattr(loop, "_resolve_history_tool_names", real_history)
    monkeypatch.setattr(abort_service, "settle_running_todos", real_todos)
    # kv_store is migration-owned rather than an ORM model. Keep the actual
    # todo/abort storage path in the SQLite fixture as well as migrated PG.
    async with get_db_session() as db:
        await db.execute(text("CREATE TABLE IF NOT EXISTS kv_store (key VARCHAR(512) PRIMARY KEY, "
                              "value TEXT NOT NULL, updated_at TIMESTAMP NOT NULL)"))

    async def no_background_provider(*args, **kwargs):
        return None

    monkeypatch.setattr(loop, "_ensure_title", no_background_provider)
    monkeypatch.setattr("agent.suggestions.generate_suggestions", no_background_provider)

    async def tools(agent, *args, **kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools} if agent.name == "assistant" else {},
                               catalogue_availability="available")

    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    # HTTP acknowledgements are deliberately not followed by an in-process
    # wake; the replacement production recovery service must discover SQL.
    monkeypatch.setattr(api, "schedule_inbox_wake", lambda *_: None)
    owner, _, workspace = await accounts()
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(session_api.router, prefix="/api")
    actor = {"user_id": owner, "workspace_id": workspace}
    app.dependency_overrides[api.get_current_user] = lambda: actor
    app.dependency_overrides[session_api.get_current_user] = lambda: actor
    app.dependency_overrides[api.get_workspace] = lambda: {"id": workspace}

    def client():
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://assistant.test")

    phase, calls, result_id, main_id = "execution", [], None, None
    stalled = asyncio.Event()
    provider_closed = asyncio.Event()

    async def stream(**kwargs):
        ctx = kwargs["ctx"]
        calls.append({"phase": phase, "session_id": ctx.session_id, "message_id": ctx.message_id,
                      "run_id": ctx.run_id, "generation": ctx.run_generation})
        count = sum(row["phase"] == phase for row in calls)
        if phase == "execution":
            yield {"type": "text_delta", "text": "Report-stop acceptance: 9 + 6 = 15. No browser or file operation was run."}
        else:
            assert ctx.session_id == main_id and ctx.sandbox is None
            assert {tool.id for tool in kwargs["tools"].values()} == REPORT_TOOLS
            if count == 1:
                wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "results.read")
                yield {"type": "tool_call", "tool": wire, "args": {"result_id": result_id},
                       "call_id": f"{phase}-original-result", "invalid": False}
                yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                return
            evidence = json.dumps(kwargs["messages"])
            assert "Compute 9 + 6" in evidence and "No browser or file operation" in evidence
            if phase == "running_stop":
                yield {"type": "text_delta", "text": "This report is still incomplete."}
                stalled.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    provider_closed.set()
                raise AssertionError("A stopped provider must not resume")
            if phase.startswith("failed_report_"):
                yield {"type": "text_delta", "text": "Incomplete report."}
                raise RuntimeError("Report provider failed after its first response")
            assert phase == "manual_report"
            yield {"type": "text_delta", "text": "The saved original execution reports 15; no browser or file operation was run."}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)

    async def run(session_id):
        lease = await reserve_run(session_id, owner)
        try:
            await loop.run_loop(session_id, user_id=owner, lease=lease)
        finally:
            await lease.release(session_status="idle")
        return lease

    service = AgentRecoveryService(interval_seconds=3600)
    await service.start()
    active_run = None
    try:
        async with client() as web:
            main = await web.post("/api/assistant/ensure", json={"model": config.model})
            assert main.status_code == 200
            main_id = main.json()["session_id"]
            snapshot_response = await web.get("/api/assistant")
            assert snapshot_response.status_code == 200, snapshot_response.text
            snapshot = snapshot_response.json()
            assert snapshot.get("state") == "ready", snapshot
            created = await web.post("/api/assistant/tasks", json={
                "idempotency_key": "report-stop-task", "project_id": snapshot["session"]["project_id"],
                "title": "Original execution retained", "input": {"text": "Compute 9 + 6 using text only.",
                                                                    "delivery": "followup"}})
            assert created.status_code == 202
            task = created.json()
            execution = await run(task["execution_session_id"])
            original_facts = await _execution_facts(task["task_id"])
            async with get_db_session() as db:
                result = await db.scalar(select(TaskResult).where(TaskResult.task_id == task["task_id"]))
                result_id, original_message = result.id, result.result_message_id
                url = db.get_bind().url.render_as_string(hide_password=False)
            first = await deliver_task_result(result_id)
            if scenario == "retry_exhaustion":
                for attempt in range(1, 4):
                    if attempt > 1:
                        assert (await deliver_task_result(result_id))["report_attempt"] == attempt
                    phase = f"failed_report_{attempt}"
                    await run(main_id)
                    async with get_db_session() as db:
                        result = await db.get(TaskResult, result_id)
                        assert result.delivery_state == ("blocked" if attempt == 3 else "retry_wait")
                        result.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                blocked_attempt, reason = 3, "retry_exhausted"
            else:
                if scenario == "running_stop":
                    phase = "running_stop"
                    active_run = asyncio.create_task(run(main_id))
                    await asyncio.wait_for(stalled.wait(), timeout=20)
                stopped = await web.post(f"/api/session/{main_id}/abort")
                assert stopped.status_code == 200 and stopped.json()["ok"]
                assert stopped.json()["canceledInbox"] == (1 if scenario == "queued_stop" else 0)
                if active_run:
                    await asyncio.wait_for(active_run, timeout=20)
                    assert provider_closed.is_set()
                blocked_attempt, reason = 1, "user_stopped"
            # Stop the prior service, close all SQL connections and construct
            # a new recovery instance instead of mocking a retry decision.
            await service.stop()
            await close_engine()
            init_engine(url)
            before_recovery_calls = list(calls)
            service = AgentRecoveryService(interval_seconds=3600)
            recovered = await service.start()
            assert recovered.resumed_inbox_sessions == 0
            assert calls == before_recovery_calls
            assert await deliver_task_result(result_id) is None
            repeated = await service.run_once()
            assert repeated.assistant_results_recovered == repeated.resumed_inbox_sessions == 0
            assert await _execution_facts(task["task_id"]) == original_facts
            result_view = (await web.get(f"/api/assistant/results/{result_id}")).json()
            assert result_view["delivery_state"] == "blocked" and result_view["last_error_code"] == reason
            assert result_view["outcome"] == "succeeded"
            assert result_view["run_id"] == execution.run_id and result_view["generation"] == execution.generation
            assert any("9 + 6 = 15" in row["text"] for row in result_view["sources"])
            task_view = (await web.get(f"/api/assistant/tasks/{task['task_id']}")).json()
            assert task_view["latest_result"]["last_error_code"] == reason

            endpoint = f"/api/assistant/results/{result_id}/retry"
            body = {"idempotency_key": "manual-report-retry", "expected_report_attempt": blocked_attempt}
            retry_backends = []
            from assistant import retry as retry_service
            original_actor_lock = retry_service.lock_actor
            both_requests = asyncio.Event()

            async def concurrent_actor_lock(db, actor_id):
                retry_backends.append(await db.scalar(text("SELECT pg_backend_pid()")))
                if len(retry_backends) == 2:
                    both_requests.set()
                await asyncio.wait_for(both_requests.wait(), timeout=10)
                return await original_actor_lock(db, actor_id)

            with monkeypatch.context() as retry_patch:
                if url.startswith("postgresql"):
                    retry_patch.setattr(retry_service, "lock_actor", concurrent_actor_lock)
                async with client() as second_device:
                    one, two = await asyncio.gather(web.post(endpoint, json=body), second_device.post(endpoint, json=body))
            if url.startswith("postgresql"):
                assert len(set(retry_backends)) == 2
            assert one.status_code == two.status_code == 202 and one.json() == two.json()
            receipt = one.json()
            assert receipt["report_attempt"] == blocked_attempt + 1
            assert await _execution_facts(task["task_id"]) == original_facts
            await service.stop()
            await close_engine()
            init_engine(url)
            phase = "manual_report"
            completed, dispatched = asyncio.Event(), []
            actual_drive = inbox._drive_claimed

            async def observe_drive(lease, batch):
                dispatched.append({"session_id": lease.session_id, "run_id": lease.run_id,
                                   "generation": lease.generation, "inbox_ids": [row.id for row in batch.receipts]})
                try:
                    await actual_drive(lease, batch)
                finally:
                    completed.set()

            monkeypatch.setattr(inbox, "_drive_claimed", observe_drive)
            service = AgentRecoveryService(interval_seconds=3600)
            resumed = await service.start()
            assert resumed.resumed_inbox_sessions == 1
            await asyncio.wait_for(completed.wait(), timeout=20)
            pending = inbox._wake_tasks.get((owner, main_id))
            if pending:
                await asyncio.wait_for(asyncio.shield(pending), timeout=20)
            assert len(dispatched) == 1 and dispatched[0]["session_id"] == main_id
            assert dispatched[0]["inbox_ids"] == [receipt["inbox_id"]]
            assert (await web.post(endpoint, json=body)).json() == receipt
            stale = await web.post(endpoint, json={**body, "idempotency_key": "new-stale-retry"})
            assert stale.status_code == 409
            assert (await web.get(f"/api/assistant/commands/{receipt['command_id']}")).json()["receipt"] == receipt
            repeated = await service.run_once()
            assert repeated.assistant_results_recovered == repeated.resumed_inbox_sessions == 0
    finally:
        await service.stop()
        if active_run is not None and not active_run.done():
            active_run.cancel()
            await asyncio.gather(active_run, return_exceptions=True)

    assert await _execution_facts(task["task_id"]) == original_facts
    assert sum(row["phase"] == "execution" for row in calls) == 1
    assert sum(row["phase"] == "manual_report" for row in calls) == 2
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        assert result.delivery_state == "processed" and result.report_attempt == blocked_attempt + 1
        assert result.result_message_id == original_message
        assert result.run_id == execution.run_id and result.generation == execution.generation
        assert result.assistant_inbox_id == receipt["inbox_id"]
        assert (await db.get(AgentDriverState, main_id)).phase == "idle"
        assert (await db.get(Message, result.processed_message_id)).finish == "stop"
        counts = {name: await db.scalar(select(func.count()).select_from(model).where(predicate))
            for name, model, predicate in (
                ("results", TaskResult, TaskResult.task_id == task["task_id"]),
                ("manual_retry_commands", AssistantCommand, (AssistantCommand.assistant_session_id == main_id)
                    & (AssistantCommand.action == "report_retry")),
                ("report_inbox", AgentInboxItem, (AgentInboxItem.session_id == main_id)
                    & (AgentInboxItem.origin == "task_result")),
                ("processed_events", AgentEvent, (AgentEvent.session_id == main_id)
                    & (AgentEvent.kind == "assistant.result.processed")),
            )}
        assert counts == {"results": 1, "manual_retry_commands": 1, "report_inbox": blocked_attempt + 1,
                          "processed_events": 1}
        witness = {"scenario": scenario, "task_id": task["task_id"], "execution_session_id": task["execution_session_id"],
            "result_id": result_id, "execution_run_id": execution.run_id, "execution_generation": execution.generation,
            "original_report_inbox": first["inbox_id"], "blocked_attempt": blocked_attempt, "blocked_reason": reason,
            "original_execution_unchanged": original_facts, "manual_retry": receipt, "counts": counts,
            "blocked_recovery": asdict(recovered), "manual_recovery": asdict(resumed), "dispatched": dispatched,
            "processed_message_id": result.processed_message_id, "provider_calls": calls,
            "running_provider_closed": provider_closed.is_set()}
        witness["manual_retry_postgres_backends"] = retry_backends
    assert (await verify_agent_event_parity(main_id, user_id=owner)).ok
    assert (await verify_agent_event_parity(task["execution_session_id"], user_id=owner)).ok
    record_property("assistant_acceptance", json.dumps(witness))
