"""Regressions from native JEV browser acceptance, using the real SQL boundary."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from memory import packet, readers
from memory.providers.jev import NATIVE_READ_QUESTIONS, validate_response
from memory.providers.common import MemoryProviderError
from memory.providers.configured_read import decode
from memory.read_contract import permits
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_memory_read_protocol import context


def decision(policy="allowed", role="none", temporal="unrestricted", **needs):
    choices = {"read_policy": policy, "restriction_role": role, "time_scope": temporal,
               "memory_needed": "skip", "task_needed": "skip"}
    answers = {}
    for name, question in NATIVE_READ_QUESTIONS.items():
        if question["type"] == "choice":
            selected = choices[name]
            answers[name] = {"type": "choice", "choice": selected, "confidence": 1,
                "probabilities": {key: int(key == selected) for key in question["criteria"]}}
        else:
            answers[name] = {"type": "noul", "noul": needs.get(name, 0)}
    result = validate_response({"model": "jev-test", "answers": answers,
        "usage": {"input_tokens": 30, "output_tokens": 30}}, "jev-test", NATIVE_READ_QUESTIONS)
    return {**result, "classifier": "jev", "protocol": "native-read-v3"}


async def native_context(monkeypatch, response, query="仅查上传的资料，不要参考我的聊天。"):
    config, turn = await context(monkeypatch)
    config.read_router_model = ""
    async def evaluate(*_):
        return deepcopy(response)
    monkeypatch.setattr("memory.read_routing.evaluate_read_needs", evaluate)
    monkeypatch.setattr("memory.providers.configured_read.evaluate", forbidden)
    return config, replace(turn, query=query)


async def forbidden(*args, **kwargs):
    pytest.fail("Unresolved or restricted scope exposed persistent content")


async def test_native_documents_only_preserves_document_permission_without_personal_reads(monkeypatch):
    config, turn = await native_context(monkeypatch, decision("documents_only", "access"))
    monkeypatch.setattr("memory.retrieval.search_memory", forbidden)
    monkeypatch.setattr("memory.orchestrator._stable_background", forbidden)
    result = await packet.prepare_context(turn, config)
    assert result["route"]["read_policy"] == "documents_only"
    assert result["route"]["classifier"] == "jev"
    assert result["_turn"].time_intent["choice"] == "none"
    assert permits(result["_turn"], "documents", bounded=True)
    assert not permits(result["_turn"], "history", bounded=True)
    assert not result["items"] and not result["stable_background"]["items"]


@pytest.mark.parametrize("role", ["none", "action", "mentioned", "withdrawn"])
async def test_non_access_role_resolves_conditional_scope_without_lowering_restriction_threshold(monkeypatch, role):
    response = decision(role=role)
    response["answers"]["read_policy"].update(confidence=.73, probabilities={
        "allowed": .78, "current_only": .19, "no_history": .02, "documents_only": 0, "uncertain": .01})
    config, turn = await native_context(monkeypatch, response)
    result = await packet.prepare_context(turn, config)
    assert result["route"]["read_policy"] == "allowed"
    assert result["route"]["policy_basis"] == "non_access_role_with_allowed_policy"
    assert result["route"]["read_policy_answer"]["confidence"] == .73
    assert not result["route"]["memory"]["needed"]


@pytest.mark.parametrize("policy,role,temporal", [
    ("current_only", "mentioned", "unrestricted"),
    ("allowed", "access", "unrestricted"),
    ("documents_only", "access", "record_filter"),
])
async def test_native_conflict_without_fallback_stops_without_reading(monkeypatch, policy, role, temporal):
    config, turn = await native_context(monkeypatch, decision(policy, role, temporal))
    config.jev_fallback_model = ""
    monkeypatch.setattr("memory.retrieval.search_memory", forbidden)
    monkeypatch.setattr("memory.readers.catalogs", forbidden)
    result = await packet.prepare_context(turn, config)
    assert result["status"] == "unavailable" and result["route"]["read_policy"] == "uncertain"
    assert not result["_turn"].deferred_read
    assert not permits(result["_turn"], "history")


def qwen_response(query, **changes):
    wire = {"g": ["none", 1], "p": ["", "allowed", 1], "m": ["skip", 1], "t": ["skip", 1],
        "n": [0, 0, 0, 0], "u": 0, "r": 0, "d": ["", "none", 1], **changes}
    return {"classifier": "configured", "model": "openai/qwen3.8-flash", "provider_model": "qwen3.8-flash",
        "provider_attempts": 1, "answers": decode(wire, query), "usage": {"input_tokens": 100, "output_tokens": 20}}


async def test_native_timeout_keeps_diagnostics_and_calls_qwen_once_with_current_input_only(monkeypatch):
    config, turn = await native_context(monkeypatch, decision(), "不要读取旧资料。")
    config.jev_timeout_seconds = .02
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def slow(*_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr("memory.read_routing.evaluate_read_needs", slow)
    monkeypatch.setattr("memory.retrieval.search_memory", forbidden)
    monkeypatch.setattr("memory.readers.catalogs", forbidden)
    calls = []
    async def fallback(state, settings, *, max_attempts):
        calls.append((state, settings, max_attempts))
        assert cancelled.is_set()
        return qwen_response(turn.query, g=["access", 1], p=[turn.query, "no_history", 1])
    monkeypatch.setattr("memory.providers.configured_read.evaluate", fallback)
    turn = replace(turn, recent_context=({"role": "user", "text": "PRIVATE PRIOR CONTENT"},))
    result = await packet.prepare_context(turn, config)
    assert started.is_set() and cancelled.is_set()
    routed = result["route"]
    assert result["status"] == "restricted" and routed["called"]
    assert routed["primary_decision"]["reason_code"] == "timeout" and routed["duration_ms"] >= 20
    assert routed["model"] == "openai/qwen3.8-flash" and routed["provider_attempts"] == 2
    assert routed["fallback_reason"] == "jev_timeout"
    assert len(calls) == 1 and calls[0][2] == 1
    assert calls[0][1].read_router_model == config.jev_fallback_model and not calls[0][1].read_router_url
    assert calls[0][0]["current_request"] == turn.query
    assert "PRIVATE PRIOR CONTENT" not in json.dumps(calls[0][0])
    assert not result["_turn"].deferred_read and not permits(result["_turn"], "history")


@pytest.mark.parametrize("cause", ["role_conflict", "date_scope", "low_confidence", "uncertain_restriction", "transport"])
async def test_qwen_resolves_native_uncertainty_before_reading(monkeypatch, cause):
    response = decision("allowed", "access") if cause == "role_conflict" else decision()
    if cause == "date_scope":
        response = decision(temporal="record_filter")
    if cause == "low_confidence":
        response["answers"]["memory_needed"]["confidence"] = .2
    if cause == "uncertain_restriction":
        response = decision("no_history", "access")
        response["answers"]["read_policy"]["confidence"] = .5
    config, turn = await native_context(monkeypatch, response, "今天想出去放松一下，去哪比较合我心意？")
    if cause == "transport":
        async def failed(*_):
            raise MemoryProviderError("provider_error", 503)
        monkeypatch.setattr("memory.read_routing.evaluate_read_needs", failed)
    calls = []
    async def fallback(state, settings, *, max_attempts):
        calls.append("qwen")
        return qwen_response(turn.query, m=["retrieve", 1], u=1)
    async def recall(**kwargs):
        assert calls == ["qwen"]
        assert kwargs["include_background"] and kwargs["retrieval_needs"]["personal_useful"] == 1
        calls.append("recall")
        return {"items": []}
    monkeypatch.setattr("memory.providers.configured_read.evaluate", fallback)
    monkeypatch.setattr("memory.retrieval.search_memory", recall)
    result = await packet.prepare_context(turn, config)
    assert calls == ["qwen", "recall"]
    assert result["route"]["read_policy"] == "allowed" and not result["_turn"].deferred_read
    assert result["route"]["usage"]["input_tokens"] == (100 if cause == "transport" else 130)


@pytest.mark.parametrize("failure", ["timeout", "invalid_response", "unknown_memory", "unknown_task", "uncertain_time"])
async def test_unresolved_second_judge_stops_without_third_stage_or_reads(monkeypatch, failure):
    config, turn = await native_context(monkeypatch, decision("uncertain", "uncertain"))
    config.jev_fallback_timeout_seconds = .02
    calls = []
    async def fallback(state, settings, *, max_attempts):
        calls.append(1)
        if failure == "timeout":
            await asyncio.Future()
        if failure == "invalid_response":
            raise MemoryProviderError("invalid_response")
        changes = {"m": ["unknown", 1]} if failure == "unknown_memory" else (
            {"t": ["skip", .2]} if failure == "unknown_task" else {"d": ["", "uncertain", 1]})
        return qwen_response(turn.query, **changes)
    monkeypatch.setattr("memory.providers.configured_read.evaluate", fallback)
    monkeypatch.setattr("memory.retrieval.search_memory", forbidden)
    monkeypatch.setattr("memory.readers.catalogs", forbidden)
    monkeypatch.setattr("memory.orchestrator._stable_background", forbidden)
    result = await packet.prepare_context(turn, config)
    assert calls == [1] and result["status"] == "unavailable"
    assert result["route"]["read_policy"] == "uncertain"
    assert not result["route"]["memory"]["needed"] and not result["route"]["task"]["needed"]
    assert not result["_turn"].deferred_read and not permits(result["_turn"], "history")


async def test_assistant_document_supplement_is_bounded_and_transient(monkeypatch):
    from assistant.reporting import ASSISTANT_TOOLS, REPORT_TOOLS
    from assistant.runtime import _authorize_tool_scope
    from assistant.policy import AssistantError
    from session.agent_event_log import strip_memory_text
    from tool.assistant_tools import assistant_tools
    from tool.tool import ToolContext
    config, turn = await native_context(monkeypatch, decision("documents_only", "access"))
    turn = replace(turn, read_policy="documents_only", time_intent={"choice": "none", "expression": "", "confidence": 1})
    ctx = ToolContext(user_id=turn.user_id, workspace_id=turn.workspace_id, session_id=turn.session_id,
        memory_turn=turn, memory_read_budget=packet.ReadBudget(), part_id="document-supplement")
    calls = []
    async def authorize(ctx, operation, arguments):
        _authorize_tool_scope({"mode": "ordinary", "tool_ids": ASSISTANT_TOOLS}, operation, arguments)
        calls.append(operation)
    monkeypatch.setattr("tool.assistant_tools.authorize_assistant_tool", authorize)
    tool = next(tool for tool in assistant_tools if tool.id == "memory.read_more")
    result = await tool.execute(tool.parameters.model_validate({"reads": [{"kind": "documents"}]}), ctx)
    payload = json.loads(result.output)
    assert calls == ["memory.read_more"] and payload["results"][0]["kind"] == "documents"
    assert result.metadata["transient_memory_refs"]["packet_read"]
    assert result.metadata["transient_memory_refs"]["operation"] == "memory.read_more"
    assert "document-supplement" in ctx.memory_read_budget.results
    hidden = strip_memory_text({"type": "tool", "tool": tool.id, "output": "PRIVATE DOCUMENT BODY",
        "metadata": result.metadata})
    assert "PRIVATE DOCUMENT BODY" not in json.dumps(hidden)
    with pytest.raises(AssistantError):
        _authorize_tool_scope({"mode": "report_only", "tool_ids": REPORT_TOOLS}, tool.id, {})
    denied = await tool.execute(tool.parameters.model_validate({"reads": [{"kind": "history"}]}), ctx)
    assert "restricted" in denied.output
    ctx.user_id = "different-user"
    from tool.memory_tools import execute_memory_read_more
    denied = await execute_memory_read_more(tool.parameters.model_validate({"reads": [{"kind": "documents"}]}), ctx)
    assert "turn_read_scope_unavailable" in denied.output


async def test_document_lookup_does_not_load_personal_background(monkeypatch):
    response = decision(knowledge_useful=1)
    response["answers"]["memory_needed"].update(choice="retrieve", confidence=1)
    config, turn = await native_context(monkeypatch, response, "这份拍摄说明的时长要求是什么？")
    observed = []
    async def search(**kwargs):
        observed.append(kwargs)
        return {"items": []}
    monkeypatch.setattr("memory.retrieval.search_memory", search)
    monkeypatch.setattr("memory.orchestrator._stable_background", forbidden)
    result = await packet.prepare_context(turn, config)
    assert observed and observed[0]["include_background"] is False
    assert observed[0]["retrieval_needs"]["knowledge_useful"] == 1
    assert result["_turn"].read_needs["personal_useful"] == 0
    assert not result["stable_background"]["items"]


async def test_oversize_history_keeps_latest_whole_messages_and_revalidates(monkeypatch):
    config, original = await context(monkeypatch)
    config.read_more_token_budget = 1024
    turn = replace(original, read_policy="allowed")
    values = {identity: {"kind": "history", "status": "complete", "items": [{
        "session_id": identity, "version": "v1", "next_offset": None,
        "message_offsets": {"new": 0, "old": 1}, "entries": [
            {"message_id": "old", "role": "assistant", "text": "Earlier details. " * 1300},
            {"message_id": "new", "role": "user", "text": f"{identity}: the final meeting is at 16:00."}]}]}
        for identity in ("one", "two")}
    async def read(request, *_):
        return deepcopy(values[request.refs[0]["id"]])
    monkeypatch.setattr(readers, "read_request", read)
    requests = [packet.ReadRequest(kind="history", refs=({"id": key, "revision": 1},)) for key in values]
    budget = packet.ReadBudget()
    result = await packet.read_more(requests, turn, budget, config)
    assert result["status"] == "partial" and budget.tokens <= 1024
    assert packet.tokens(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 1024
    for value in result["results"]:
        window = value["items"][0]
        assert [entry["message_id"] for entry in window["entries"]] == ["new"]
        assert window["next_offset"] == 1
    args = {"reads": [{"kind": "history", "refs": request.refs} for request in requests]}
    assert await readers.revalidate_result(args, result, turn, config) == result
    values["one"]["items"][0]["entries"][-1]["text"] = "The corrected time is 17:00."
    assert (await readers.revalidate_result(args, result, turn, config))["reason"] == "evidence_changed"


async def test_empty_overbudget_page_allows_one_narrower_retry_with_shared_token_budget(monkeypatch):
    config, original = await context(monkeypatch)
    config.read_more_token_budget = 1024
    turn = replace(original, read_policy="allowed")
    async def read(request, *_):
        return {"kind": "sources", "status": "complete", "items": [{"id": "source", "revision": 1,
            "text": "whole indivisible evidence " * (2000 if request.max_chars > 100 else 5)}]}
    monkeypatch.setattr(readers, "read_request", read)
    budget = packet.ReadBudget()
    first = await packet.read_more([packet.ReadRequest(kind="sources")], turn, budget, config)
    assert first["status"] == "partial" and first["continuation_available"]
    second = await packet.read_more([packet.ReadRequest(kind="sources", max_chars=100)], turn, budget, config)
    assert second["results"][0]["items"] and budget.tokens <= 1024
    assert (await packet.read_more([packet.ReadRequest()], turn, budget, config))["status"] == "budget_exhausted"


async def test_history_catalog_excludes_current_and_isolates_then_ranks_before_recent_limit(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from db.base import get_db_session
    from db.models.session import Session
    config, original = await context(monkeypatch)
    turn = replace(original, query="天文观测的场地", read_policy="allowed")
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        main = await db.get(Session, turn.session_id)
        # An active query must never appear as the answer's historical source.
        main.kind, main.title, main.memory_policy = "normal", turn.query, "standard"
        turn = replace(turn, project_id=main.project_id, include_all_projects=False)
        for i in range(10):
            db.add(Session(id=f"history-{i}", user_id=turn.user_id, workspace_id=turn.workspace_id,
                project_id=main.project_id, title="天文观测地点" if i in {8, 9} else f"无关采购 {i}",
                kind="normal", memory_policy="isolated" if i == 9 else "standard",
                created_at=now-timedelta(days=i+1), updated_at=now-timedelta(days=i+1)))
    result = await readers.catalogs(turn, {"history_useful": 1}, config)
    assert result[0]["id"] == "history-8"
    assert turn.session_id not in {row["id"] for row in result}
    assert "history-9" not in {row["id"] for row in result} and len(result) == 6


async def test_history_without_selected_session_is_partial_and_can_be_corrected(monkeypatch):
    config, original = await context(monkeypatch)
    result = await packet.read_more([packet.ReadRequest(kind="history", query="过去的方案")],
        replace(original, read_policy="allowed"), packet.ReadBudget(), config)
    assert result["status"] == "partial" and result["continuation_available"]
    assert result["results"][0]["reason"] == "history_reference_required"


def test_history_catalog_survives_generic_fact_summaries_within_same_budget():
    from core.config import OpenBoxConfig
    from memory.orchestrator import render_memory_context
    config = OpenBoxConfig().memory
    catalog = [{"kind": "history", "id": f"past-{i}", "title": f"原来的计划 {i}"} for i in range(4)]
    bundle = {"_semantic_packet": True, "settings": {},
        "route": {"read_policy": "allowed", "needs": {"history_useful": 1}},
        "items": [{"kind": "memory", "id": str(i), "revision": 1, "text": "通用偏好"} for i in range(8)],
        "stable_background": {"items": []}, "known_gaps": [], "degraded_reasons": [], "catalogs": list(catalog)}
    packet.apply_budget(bundle, config)
    assert bundle["catalogs"] == catalog
    assert len(bundle["catalogs"]) + len(bundle["items"]) <= config.recall_item_limit
    assert packet.tokens(render_memory_context(bundle)) <= config.recall_token_budget


async def test_complete_page_allows_one_different_original_but_not_same_read_loop(monkeypatch):
    config, original = await context(monkeypatch)
    turn = replace(original, read_policy="allowed")
    calls = []
    async def read(request, *_):
        calls.append(request)
        return {"kind": "history", "status": "complete", "items": []}
    monkeypatch.setattr(readers, "read_request", read)
    budget = packet.ReadBudget()
    first = packet.ReadRequest(kind="history", refs=({"id": "lookup", "revision": 1},))
    assert (await packet.read_more([first], turn, budget, config))["continuation_available"]
    assert (await packet.read_more([first], turn, budget, config))["status"] == "budget_exhausted"
    second = packet.ReadRequest(kind="history", refs=({"id": "original", "revision": 1},))
    assert not (await packet.read_more([second], turn, budget, config))["continuation_available"]
    assert (await packet.read_more([packet.ReadRequest()], turn, budget, config))["status"] == "budget_exhausted"
    assert calls == [first, second] and budget.tokens <= config.read_more_token_budget
