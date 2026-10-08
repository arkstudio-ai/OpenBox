"""PA-16: busy real main loops separate continuous humans and actual reports."""
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from sqlalchemy import select

from agent import inbox, loop, processor
from agent.driver import reserve_run
from assistant import budget
from assistant.commands import accept_task_command
from assistant.delivery import recover_assistant_results
from assistant.inputs import accept_turn
from assistant.reporting import ASSISTANT_TOOLS, REPORT_TOOLS
from assistant.results import deliver_task_result
from assistant.service import ensure_main_session
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult
from db.models.message import Message
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_report_recovery_acceptance import _execution_facts
from tool.assistant_tools import assistant_tools


async def test_busy_mixed_queue_keeps_fair_read_only_report_retries(monkeypatch, record_property):
    config = _loop_config()
    config.compaction.auto = False
    config.permission = {"*": "allow"}
    config.assistant.ordinary.model_requests = 1
    config.assistant.report_only.model_requests = 1
    history = loop._resolve_history_tool_names
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    monkeypatch.setattr(loop, "_resolve_history_tool_names", history)

    async def no_background_provider(*args, **kwargs):
        return None

    async def tools(agent, *args, **kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools}
            if agent.name == "assistant" else {}, catalogue_availability="available")

    monkeypatch.setattr(loop, "_ensure_title", no_background_provider)
    monkeypatch.setattr("agent.suggestions.generate_suggestions", no_background_provider)
    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    executions, results, humans, arrivals, turns = {}, {}, [], [], []
    provider_calls = Counter()
    active_lease = None

    async def human():
        index = len(humans)
        receipt = await accept_turn(user_id=owner, workspace_id=workspace, main_id=main.id,
            client_id=f"mixed-human-{index}", text=f"PA16_LIVE_HUMAN_{index}: acknowledge only.")
        humans.append(receipt["inbox_id"])
        return receipt["inbox_id"]

    async def provider(**kwargs):
        ctx = kwargs["ctx"]
        if ctx.session_id != main.id:
            assert ctx.session_id in executions
            provider_calls[ctx.session_id] += 1
            yield {"type": "text_delta", "text": f"PA16_RESULT_{executions[ctx.session_id]}: text-only original work complete."}
            yield {"type": "finish", "reason": "stop", "usage": {}}
            return
        async with get_db_session() as db:
            claimed = list((await db.scalars(select(AgentInboxItem).where(
                AgentInboxItem.session_id == main.id, AgentInboxItem.state == "claimed"))).all())
            assert len(claimed) == 1
            item = claimed[0]
            assert (item.run_id, item.generation) == (ctx.run_id, ctx.run_generation)
            origin, item_id, ref = item.origin, item.id, dict(item.origin_ref)
        assert not (await inbox.claim_inbox_boundary(active_lease, step=99, include_next_turn=False)).receipts
        provider_calls[item_id] += 1
        first = provider_calls[item_id] == 1
        if first:
            turns.append({"inbox_id": item_id, "origin": origin, "run_id": ctx.run_id,
                "generation": ctx.run_generation, "result_id": ref.get("result_id"),
                "report_attempt": ref.get("report_attempt")})
            # New input arrives while a real provider step owns this main run.
            arrivals.append({"during_run": ctx.run_id, "during_origin": origin,
                "new_human_inbox": await human()})
        tool_ids = {tool.id for tool in kwargs["tools"].values()}
        payload = json.dumps(kwargs["messages"], ensure_ascii=False)
        if origin == "human":
            assert tool_ids == ASSISTANT_TOOLS and ctx.sandbox is None
            assert first  # Ordinary request cap=1; no extra provider round.
            label = "A" if item_id == humans[0] else "B" if item_id == humans[1] else None
            if label:
                delivered = await deliver_task_result(results[label])
                assert delivered and delivered["report_attempt"] == 1
                arrivals[-1]["new_report_inbox"] = delivered["inbox_id"]
            yield {"type": "text_delta", "text": "Your new input was acknowledged."}
        else:
            assert origin == "task_result" and tool_ids == REPORT_TOOLS and ctx.sandbox is None
            assert "PA16_LIVE_HUMAN_" not in payload
            result_id = ref["result_id"]
            assert result_id in results.values()
            if first:
                wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "results.read")
                yield {"type": "tool_call", "tool": wire, "args": {"result_id": result_id},
                    "call_id": f"read-{item_id}", "invalid": False}
                yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                return
            label = next(name for name, identity in results.items() if identity == result_id)
            assert f"PA16_RESULT_{label}" in payload
            assert provider_calls[item_id] == 2
            yield {"type": "text_delta", "text": f"The original report says PA16_RESULT_{label} completed text-only work."}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", provider)

    async def run(session_id):
        nonlocal active_lease
        active_lease = await reserve_run(session_id, owner)
        try:
            await asyncio.wait_for(loop.run_loop(session_id, user_id=owner, lease=active_lease), 20)
        finally:
            await active_lease.release(session_status="idle")
            active_lease = None
        assert budget.current.get() is None

    tasks, original_facts = {}, {}
    for label in ("A", "B"):
        task = await accept_task_command(user_id=owner, workspace_id=workspace, main_id=main.id,
            project_id=main.project_id, idempotency_key=f"mixed-task-{label}",
            prompt=f"Produce PA16_RESULT_{label} with text only; no external work.")
        tasks[label] = task
        executions[task["execution_session_id"]] = label
        await run(task["execution_session_id"])
        async with get_db_session() as db:
            result = await db.scalar(select(TaskResult).where(TaskResult.task_id == task["task_id"]))
            assert result.delivery_state == "pending" and result.report_attempt == 1
            results[label] = result.id
        original_facts[label] = await _execution_facts(task["task_id"])
    await human()
    retried = False
    waiting = []
    failed_report = None
    for _ in range(12):
        async with get_db_session() as db:
            modes = set((await db.scalars(select(AgentInboxItem.origin).where(
                AgentInboxItem.session_id == main.id, AgentInboxItem.state == "accepted"))).all())
        waiting.append(sorted(modes))
        await run(main.id)
        if modes == {"human", "task_result"}:
            if len(turns) >= 3 and turns[-3]["origin"] == turns[-2]["origin"] == "human":
                assert turns[-1]["origin"] == "task_result"
            if len(turns) >= 2 and turns[-2]["origin"] == "task_result":
                assert turns[-1]["origin"] == "human"
        async with get_db_session() as db:
            result = await db.get(TaskResult, results["A"])
            if not retried and result.delivery_state == "retry_wait":
                assert result.processed_message_id is None and result.report_attempt == 1
                failed = await db.get(AgentInboxItem, result.assistant_inbox_id)
                failure = await db.get(Message, failed.result_message_id)
                assert failed.outcome == "error" and failure.error["code"] == budget.CODE
                failed_report = failed.id
                assert provider_calls[failed.id] == 1
                result.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            complete = all([(await db.get(TaskResult, identity)).delivery_state == "processed"
                            for identity in results.values()])
        if failed_report and not retried:
            # Persisted queue history and the original failed receipt survive
            # fresh connections. Only the new report attempt gets a new cap.
            url = get_engine().url
            await close_engine()
            init_engine(url)
            config.assistant.report_only.model_requests = 2
            assert await recover_assistant_results(result_ids=(results["A"],)) == 1
            retried = True
        if complete:
            break
    else:
        raise AssertionError("Continuous human input starved a real report")
    assert retried and failed_report
    assert sum(modes == ["human", "task_result"] for modes in waiting) >= 4
    reports = [turn for turn in turns if turn["origin"] == "task_result"]
    assert [(turn["result_id"], turn["report_attempt"]) for turn in reports] == [
        (results["A"], 1), (results["B"], 1), (results["A"], 2)]
    assert [turn["inbox_id"] for turn in turns if turn["origin"] == "human"] == humans[:len(turns) - 3]
    assert all(provider_calls[sid] == 1 for sid in executions)
    for label, task in tasks.items():
        assert await _execution_facts(task["task_id"]) == original_facts[label]
        assert (await verify_agent_event_parity(task["execution_session_id"], user_id=owner)).ok
    async with get_db_session() as db:
        claims = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
            AgentEvent.kind == "assistant.queue.claimed").order_by(AgentEvent.sequence))).all())
        assert [event.payload["inbox_id"] for event in claims] == [turn["inbox_id"] for turn in turns]
        starts = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
            AgentEvent.kind == "assistant.budget.started").order_by(AgentEvent.sequence))).all())
        assert len(starts) == len(turns)
        for label, result_id in results.items():
            result = await db.get(TaskResult, result_id)
            assert result.report_attempt == (2 if label == "A" else 1)
            assert (await db.get(Message, result.processed_message_id)).finish == "stop"
        assert await db.scalar(select(AgentInboxItem.id).where(
            AgentInboxItem.session_id == main.id, AgentInboxItem.state == "claimed")) is None
        assert await db.scalar(select(AgentInboxItem.id).where(
            AgentInboxItem.session_id == main.id, AgentInboxItem.origin == "human",
            AgentInboxItem.state == "accepted")) is not None
    assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
    record_property("assistant_acceptance", json.dumps({"scenario": "PA-16", "main_id": main.id,
        "tasks": tasks, "results": results, "turns": turns, "waiting_classes_before_turn": waiting,
        "arrivals_during_provider": arrivals, "failed_report_inbox": failed_report,
        "provider_calls": dict(provider_calls), "execution_unchanged": original_facts,
        "budget_starts": [{"run_id": event.run_id, "payload": event.payload} for event in starts],
        "report_tools_only": True, "report_provider_excludes_all_live_human_canaries": True}))
