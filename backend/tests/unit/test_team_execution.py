"""Frozen generation options reach the actual request boundary."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest
from pydantic import ValidationError

from agent_catalog.compiler import compile_agent
from team.errors import TeamError
from team.execution import output_limit
from team.runtime_binding import RuntimeBinding, _current
from tests.unit.test_team_compiler import spec
from tests.unit.test_subagent_composition import _config
from tests.unit.test_team_paid_tools import paid
from tool.tool import ToolContext


@pytest.mark.parametrize("options", [
    {"temperature": 0}, {"top_p": 0.5}, {"max_output_tokens": True},
    {"max_output_tokens": 0}, {"max_output_tokens": 12.5},
])
def test_unsupported_or_invalid_generation_options_are_not_silently_saved(options):
    with pytest.raises(ValidationError):
        spec(generation_options=options)


def test_output_cap_is_frozen_and_only_narrows_the_request():
    definition = spec(generation_options={"max_output_tokens": 1200})
    frozen = compile_agent(definition, config=_config("openai/test")).snapshot()
    definition.generation_options["max_output_tokens"] = 2400
    assert output_limit(frozen["spec"], "openai/test", None, 4000) == 1200
    assert output_limit(frozen["spec"], "openai/test", None, 300) == 300


def test_reasoning_cannot_expand_past_an_explicit_output_cap(monkeypatch):
    monkeypatch.setattr("agent.llm._get_variant_kwargs", lambda *_: {"thinking": {"budget_tokens": 2000}})
    with pytest.raises(TeamError, match="reasoning allowance"):
        compile_agent(spec(generation_options={"max_output_tokens": 1200}), config=_config("openai/test"))


@pytest.mark.parametrize("responses", [False, True])
async def test_stream_and_budget_receive_the_same_frozen_output_cap(monkeypatch, responses):
    from agent import llm
    recorded = []
    async def provider(*_args, **kwargs):
        recorded.append(kwargs["max_output_tokens"])
        yield {"type": "finish", "reason": "stop", "usage": {"output": 1}}
    bound = AsyncMock(return_value=None)
    monkeypatch.setattr("team.budget.before_model_call", bound)
    monkeypatch.setattr("billing.service.UsageMeter.start", AsyncMock(return_value=None))
    monkeypatch.setattr(llm, "_needs_responses_api", lambda _: responses)
    monkeypatch.setattr(llm, "_stream_responses_api", provider)
    monkeypatch.setattr(llm, "_stream_litellm_direct", provider)
    definition = spec(generation_options={"max_output_tokens": 1200}).model_dump(mode="json")
    token = _current.set(RuntimeBinding(None, "trial", "owner", "workspace", "project", "trial", definition, {}))
    try:
        ctx = ToolContext(session_id="trial")
        events = [event async for event in llm.stream_llm(None, [], [], {}, "openai/test", ctx, max_output_tokens=8000)]
    finally:
        _current.reset(token)
    assert events[-1]["type"] == "finish"
    assert recorded == [1200]
    assert bound.await_args.kwargs["output_tokens"] == 1200


async def test_auxiliary_completion_also_respects_the_definition_cap(monkeypatch):
    from agent import llm
    provider = AsyncMock(return_value=SimpleNamespace(usage=None))
    capture = SimpleNamespace(context=None, chunk=AsyncMock(), finish=AsyncMock())
    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr("team.budget.before_model_call", AsyncMock(return_value=None))
    monkeypatch.setattr("billing.service.UsageMeter.start", AsyncMock(return_value=None))
    monkeypatch.setattr(llm.RequestCapture, "start", AsyncMock(return_value=capture))
    token = _current.set(RuntimeBinding(None, "trial", "owner", "workspace", "project", "trial",
        spec(generation_options={"max_output_tokens": 1200}).model_dump(mode="json"), {}))
    try:
        await llm.metered_completion(ctx=ToolContext(session_id="trial"), billing_kind="title",
            model="openai/test", max_completion_tokens=3000, messages=[])
    finally:
        _current.reset(token)
    assert provider.await_args.kwargs["max_completion_tokens"] == 1200


async def test_auxiliary_request_waits_until_stream_settlement_before_budget_admission(monkeypatch):
    from agent import llm
    entered, release, settlement = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    async def provider(*_args, **_kwargs):
        calls.append("stream")
        entered.set()
        await release.wait()
        yield {"type": "finish", "reason": "stop", "usage": {"output": 1}}
    class Meter:
        finished = False
        async def finish(self, _usage):
            calls.append("settling")
            await settlement.wait()
            self.finished = True
            calls.append("settled")
            return 0
    async def bound(*_, **__):
        calls.append("budget")
    async def aux_provider(**_kwargs):
        calls.append("aux")
        return SimpleNamespace(usage=None)
    capture = SimpleNamespace(context=None, chunk=AsyncMock(), finish=AsyncMock())
    monkeypatch.setattr(llm, "_needs_responses_api", lambda _: False)
    monkeypatch.setattr(llm, "_stream_litellm_direct", provider)
    monkeypatch.setattr("litellm.acompletion", aux_provider)
    monkeypatch.setattr(llm.RequestCapture, "start", AsyncMock(return_value=capture))
    monkeypatch.setattr(llm, "capture_billing", AsyncMock())
    monkeypatch.setattr("team.budget.before_model_call", bound)
    monkeypatch.setattr("billing.service.UsageMeter.start", AsyncMock(side_effect=[Meter(), None]))
    token = _current.set(RuntimeBinding(None, "trial", "owner", "workspace", "project", "trial", {}, {}))
    try:
        async def consume():
            return [event async for event in llm.stream_llm(None, [], [], {}, "openai/test", ToolContext(session_id="trial"))]
        streamed = asyncio.create_task(consume())
        await entered.wait()
        auxiliary = asyncio.create_task(llm.metered_completion(ctx=ToolContext(session_id="trial"),
            billing_kind="title", model="openai/test", max_tokens=100, messages=[]))
        release.set()
        await asyncio.sleep(0)
        assert calls == ["budget", "stream", "settling"]
        assert not auxiliary.done()
        settlement.set()
        await asyncio.gather(streamed, auxiliary)
        assert calls == ["budget", "stream", "settling", "settled", "budget", "aux"]
    finally:
        release.set()
        settlement.set()
        _current.reset(token)


def test_tool_category_limit_is_validated_at_compilation():
    with pytest.raises(ValidationError):
        spec(execution_policy={"tool_categories": ["unknown"]})
    with pytest.raises(TeamError, match="categories"):
        compile_agent(spec(execution_policy={"tool_categories": ["T1"]}), config=_config("openai/test"))
    assert compile_agent(spec(execution_policy={"tool_categories": ["T0"]}),
        config=_config("openai/test")).summary["tool_tiers"] == {"read": "T0", "grep": "T0"}


async def test_expired_attempt_stops_model_and_tools_and_retains_paid_reservation(paid, monkeypatch):
    from datetime import datetime, timedelta
    from team import execution, journal, scheduler
    from team.runtime_binding import assert_tool_current
    from tests.unit.test_team_paid_tools import job_for, reserve
    job = await job_for(paid)
    reservation = await reserve(paid, job)
    initial = await journal.snapshot(paid.run, paid.actor)
    started = datetime.fromisoformat(initial["attempts"][paid.attempt["id"]]["started_at"])
    now = started + timedelta(seconds=1801)
    monkeypatch.setattr(journal, "utcnow", lambda: now)
    monkeypatch.setattr(scheduler, "schedule", lambda *_: None)
    lease = SimpleNamespace(generation=1, abort=asyncio.Event(), assert_current=AsyncMock())
    watch = await execution.start_deadline_watch(lease)
    try:
        await asyncio.wait_for(watch, timeout=2)
        assert lease.abort.is_set()
        state = await journal.snapshot(paid.run, paid.actor)
        assert state["run"]["state"] == "pausing"
        assert state["run"]["pause_reason"] == "agent_wall_time_exceeded"
        assert state["reservations"][reservation.id]["state"] == "reserved"
        with pytest.raises(TeamError) as error:
            await assert_tool_current("video_generate", paid.ctx)
        assert error.value.code == "AGENT_TIME_LIMIT"
        provider = AsyncMock()
        monkeypatch.setattr("litellm.acompletion", provider)
        from agent.llm import metered_completion
        with pytest.raises(TeamError, match="time limit"):
            await metered_completion(ctx=paid.ctx, billing_kind="chat", model="openai/test", messages=[])
        provider.assert_not_called()
    finally:
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)
        execution._execution_window.set(None)


async def test_recovery_enforces_attempt_deadline_even_while_member_waits(paid):
    from datetime import datetime, timedelta
    from db.base import get_db_session
    from team.execution import expired_members
    from team.journal import snapshot
    state = await snapshot(paid.run, paid.actor)
    attempt = state["attempts"][paid.attempt["id"]]
    state["members"][attempt["member_id"]]["execution_state"] = "waiting"
    started = datetime.fromisoformat(attempt["started_at"])
    async with get_db_session() as db:
        assert await expired_members(db, state, {}, started + timedelta(seconds=1799)) == set()
        assert await expired_members(db, state, {}, started + timedelta(seconds=1800)) == {attempt["member_id"]}


@pytest.mark.parametrize("code,reason", [("INSUFFICIENT_CREDITS", "insufficient_credits"), ("MODEL_UNPRICED", "model_unpriced")])
async def test_account_billing_failure_never_calls_model_and_pauses_team(paid, monkeypatch, code, reason):
    from agent import llm
    from billing.service import BillingError
    from team import scheduler
    from team.journal import snapshot
    monkeypatch.setattr("billing.service.UsageMeter.start", AsyncMock(side_effect=BillingError(code, "Account gate refused")))
    monkeypatch.setattr(scheduler, "schedule", lambda *_: None)
    provider = AsyncMock()
    monkeypatch.setattr("litellm.acompletion", provider)
    with pytest.raises(TeamError) as error:
        await llm.metered_completion(ctx=paid.ctx, billing_kind="chat", model="openai/test", messages=[])
    assert error.value.code == code
    provider.assert_not_called()
    state = await snapshot(paid.run, paid.actor)
    assert state["run"]["state"] == "pausing"
    assert state["run"]["pause_reason"] == reason
