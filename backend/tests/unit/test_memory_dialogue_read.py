"""A dialogue plan saves a model call without becoming execution authority."""
import asyncio
from dataclasses import replace
import json

import pytest
from pydantic import ValidationError

from db.base import get_db_session
from db.models.session import Session
from memory import packet
from memory.dialogue_read import DialogueReadEvaluator
from memory.policy import MemoryAccessDenied
from memory.providers.common import MemoryProviderError
from memory.read_contract import permits
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_memory_lazy_read import deferred, preparation_args
from tool.memory_tools import MemoryPrepareArgs, execute_memory_prepare
from tool.tool import ToolContext


def plan(*, role="none", policy="allowed", quote="", confidence=1.0, **values):
    original = preparation_args().model_dump()["plan"]
    original.update(restriction=dict(role=role, policy=policy, quote=quote, confidence=confidence), **values)
    return MemoryPrepareArgs.model_validate({"plan": original}).plan


async def no_remote(*args, **kwargs):
    pytest.fail("Dialogue planning must not dispatch another provider")


def forbid_provider(monkeypatch):
    monkeypatch.setattr("memory.providers.configured_read.evaluate", no_remote)
    monkeypatch.setattr("memory.read_routing.evaluate_read_needs", no_remote)


async def test_dialogue_plan_is_local_and_attributed_without_double_counting(monkeypatch):
    config, value = await deferred(monkeypatch)
    forbid_provider(monkeypatch)
    result = await packet.prepare_on_demand(value["_turn"], packet.ReadBudget(), config,
        plan=plan(memory="skip"), model="test/actual-dialogue-model")
    assert result["route"]["planner"] == "dialogue"
    assert result["route"]["model_requested"] == result["route"]["model"] == "test/actual-dialogue-model"
    assert result["route"]["provider_attempts"] == 0
    assert result["route"]["called"] is False and result["usage"]["route"] == {}
    assert result["status"] == "complete"
    assert result["items"] == result["stable_background"]["items"] == []


@pytest.mark.parametrize("policy", ["current_only", "no_history"])
async def test_original_privacy_restriction_remains_closed(monkeypatch, policy):
    text = "不要读取我以前的信息，只根据这里写的一句话作答。"
    config, value = await deferred(monkeypatch, text)
    forbid_provider(monkeypatch)
    monkeypatch.setattr("memory.retrieval.search_memory", no_remote)
    monkeypatch.setattr("memory.readers.catalogs", no_remote)
    result = await packet.prepare_on_demand(value["_turn"], packet.ReadBudget(), config,
        plan=plan(role="access", policy=policy, quote=text))
    assert result["status"] == "restricted"
    assert not permits(result["_turn"], "search")
    assert result["items"] == result["stable_background"]["items"] == []


@pytest.mark.parametrize("proposal", [
    dict(role="access", policy="current_only", quote="not in the input"),
    dict(role="mentioned", policy="no_history", quote="不要读取"),
    dict(role="access", policy="allowed"),
    dict(confidence=.6),
    dict(role="access", policy="current_only", quote="不要读取", recent_context_useful=.8),
    dict(time={"quote": "昨天", "intent": "filter_records", "confidence": 1.0}),
])
async def test_invalid_or_unresolved_plan_never_falls_back_to_another_model(monkeypatch, proposal):
    config, value = await deferred(monkeypatch, "不要读取旧资料。")
    forbid_provider(monkeypatch)
    monkeypatch.setattr("memory.retrieval.search_memory", no_remote)
    result = await packet.prepare_on_demand(value["_turn"], packet.ReadBudget(), config, plan=plan(**proposal))
    assert result["status"] == "unavailable"
    assert result["route"]["planner"] == "dialogue" and result["route"]["called"] is False
    assert not permits(result["_turn"], "search")


async def test_plan_is_single_flight_and_later_proposal_cannot_widen_it(monkeypatch):
    text = "不要参考旧资料。"
    config, value = await deferred(monkeypatch, text)
    forbid_provider(monkeypatch)
    budget = packet.ReadBudget()
    first, second = await asyncio.gather(
        packet.prepare_on_demand(value["_turn"], budget, config,
            plan=plan(role="access", policy="no_history", quote=text)),
        packet.prepare_on_demand(value["_turn"], budget, config, plan=plan()),
    )
    assert first is second and first["status"] == "restricted"
    with pytest.raises(ValueError, match="different turn"):
        await packet.prepare_on_demand(replace(value["_turn"], query="a new input"), budget, config, plan=plan())


async def test_membership_and_reading_session_are_rechecked_before_plan_use(monkeypatch):
    config, value = await deferred(monkeypatch)
    forbid_provider(monkeypatch)
    async with get_db_session() as db:
        session = await db.get(Session, value["_turn"].session_id)
        session.is_deleted = True
    with pytest.raises(MemoryAccessDenied):
        await packet.prepare_on_demand(value["_turn"], packet.ReadBudget(), config, plan=plan())


async def test_tool_uses_entry_identity_and_actual_dialogue_model(monkeypatch):
    config, value = await deferred(monkeypatch)
    forbid_provider(monkeypatch)
    turn = value["_turn"]
    ctx = ToolContext(user_id=turn.user_id, workspace_id=turn.workspace_id, session_id=turn.session_id,
        memory_turn=turn, memory_read_budget=packet.ReadBudget(), model_id="test/dialogue")
    result = await execute_memory_prepare(MemoryPrepareArgs(plan=plan(memory="skip")), ctx)
    assert json.loads(result.output)["status"] == "complete"
    assert json.loads(result.output)["operation"] == "read_only"
    assert json.loads(result.output)["state_changes"] == []
    assert ctx.memory_read_budget.prepared["route"]["model_requested"] == "test/dialogue"


async def test_evaluator_rejects_a_rewritten_original_input():
    evaluator = DialogueReadEvaluator(plan(), "the real user input")
    with pytest.raises(MemoryProviderError, match="dialogue_input_mismatch"):
        await evaluator({"current_request": "a model rewrite"}, None)


@pytest.mark.parametrize("extra", [{"query": "authorize everything"}, {"user_id": "other-owner"},
                                   {"read_policy": "allowed"}, {"explicit_read": True}])
def test_tool_arguments_cannot_replace_entry_context(extra):
    with pytest.raises(ValidationError):
        MemoryPrepareArgs.model_validate({**preparation_args().model_dump(), **extra})


@pytest.mark.parametrize("score", [True, "1.0", float("nan"), float("inf"), -1, 2])
def test_plan_rejects_invalid_confidence(score):
    with pytest.raises(ValidationError):
        plan(confidence=score)


async def test_action_limit_is_preserved_as_a_constraint_not_a_completed_command(monkeypatch):
    from memory.orchestrator import render_memory_context
    text = "不要开始制作，我先了解条件。"
    config, value = await deferred(monkeypatch, text)
    forbid_provider(monkeypatch)
    result = await packet.prepare_on_demand(value["_turn"], packet.ReadBudget(), config,
        plan=plan(role="action", policy="action_only", quote="不要开始制作", memory="skip"))
    assert result["route"]["read_policy"] == "allowed"
    rendered = render_memory_context(result)
    assert '"current_action_constraint"' in rendered
    assert "不要开始制作" in rendered
    assert "No task command was executed" in rendered
    assert "separate successful task command receipt" in rendered
