"""Kernel guard: SQL statements and commits per agent turn stay within budget.

The real run_loop and persistence run against the migration-backed SQLite
database of the durable-question fixtures. The provider answers instantly;
there is no sandbox, and the system prompt and tool catalogue are stubbed. The
statement and commit counts are deterministic, unlike wall time, so they are
the gate (docs/KERNEL_DSH_COMPARISON_20261128.md, sections 13 and 25.2, 0-3).
Timer-driven SQL (abort polling, hold observation, streaming checkpoints) is
held off so that only the turn's own work is counted. Part of `make check-kernel`.

Two scenarios, each measured on a text-only turn and on a turn with one tool call:

- baseline: long-term memory off and trajectory recording off.
- production: the memory flags production runs with (write, extraction, index
  sync, recall, JEV routing, rerank, wiki, backfill; semantic_recall off) and
  trajectory recording on. Offline stand-ins: the JEV router answers
  "retrieve" without a network call, the user has no stored memories (so no
  embedding, Qdrant or rerank call is made), and recording goes to the test
  spool directory. Any outbound HTTP attempt fails the test.

Updating the budget: run
    cd backend && uv run --extra test pytest tests/unit/test_kernel_turn_sql_budget.py -q -s
which prints the measured counts. When a change saves SQL, lower BUDGET to the
new numbers in the same change. Raise a number only with the reason in the
change description. To see which statements changed, set
KERNEL_SQL_BUDGET_DUMP=<directory> before and after the change and diff the
per-turn files it writes.
"""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import event

import db.base as database
from tests.unit.test_durable_questions import state  # noqa: F401  (migration-backed database)

# (SQL statements, commits) for the measured turn. Measured 2026-11-28 on
# kernel-refactor/round1 (a9da02c8).
BUDGET = {
    ("baseline", "text"): (236, 66),
    ("baseline", "tool"): (374, 109),
    ("production", "text"): (275, 74),
    ("production", "tool"): (414, 118),
}
# Turns run before the measured one; the counts do not grow with history.
WARMUP_TURNS = 2

PRODUCTION_MEMORY = dict(v2_write=True, auto_extract=True, index_sync=True, retrieval_v2=True,
                         route_jev=True, rerank=True, wiki=True, backfill=True, semantic_recall=False)


@pytest.fixture(autouse=True)
def sqlite_only(monkeypatch):
    """The budget is measured on SQLite; a PostgreSQL run would count other statements."""
    monkeypatch.delenv("OBX_QUESTION_TEST_DATABASE_URL", raising=False)


@pytest.fixture
def counted(state):
    """SQL statements (transaction control excluded) and commits sent while the test runs."""
    seen = SimpleNamespace(statements=0, commits=0, text=[])

    def on_statement(conn, cursor, statement, parameters, context, executemany):
        seen.statements += 1
        seen.text.append(" ".join(statement.split()))

    def on_commit(conn):
        seen.commits += 1
    engine = database._engine.sync_engine
    event.listen(engine, "before_cursor_execute", on_statement)
    event.listen(engine, "commit", on_commit)
    yield seen
    event.remove(engine, "before_cursor_execute", on_statement)
    event.remove(engine, "commit", on_commit)


@pytest.fixture
def no_network(monkeypatch):
    """Every outbound HTTP request fails at once and is recorded."""
    attempts = []

    async def refuse(self, request, *args, **kwargs):
        attempts.append(str(request.url))
        raise httpx.ConnectError("network is off in the SQL budget test", request=request)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    return attempts


def _harness(monkeypatch, scenario: str):
    """The real agent loop with a scripted provider and no sandbox.

    The configuration starts from the defaults, not from openbox.json or the
    environment, so every checkout measures the same thing.
    """
    import litellm
    from agent import llm, loop, processor
    from agent.agent import AgentDef
    from agent.tool_resolution import ResolvedStepTools
    from core.config import MemoryConfig, OpenBoxConfig

    config = OpenBoxConfig()
    config.tool_exposure.mode = "legacy_eager"
    config.model = "openai/gpt-4o"
    config.models = []
    config.permission = {"*": "allow"}
    config.jwt_secret = ""
    if scenario == "production":
        config.memory = MemoryConfig(**PRODUCTION_MEMORY)
        monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    else:
        config.memory = MemoryConfig()
        monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    monkeypatch.delenv("TRAJECTORY_RECORD_USER_IDS", raising=False)
    monkeypatch.setattr("core.config._config", config)
    monkeypatch.setattr("core.config.get_config", lambda: config)
    harness = SimpleNamespace(loop=loop, processor=processor, tools={}, routed=[])

    async def no_sandbox(*args, **kwargs):
        return None

    async def system(*args, **kwargs):
        return ["Test conversation"]

    async def tools(*args, **kwargs):
        return ResolvedStepTools(tools=dict(harness.tools), catalogue_availability="available")

    async def no_title(**kwargs):
        raise RuntimeError("title generation is out of scope")
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", no_sandbox)
    monkeypatch.setattr("sandbox.entitlement.subscription_sandbox_enabled", lambda: False)
    monkeypatch.setattr(loop, "_build_system_prompt", system)
    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    monkeypatch.setattr(loop, "get_agent", lambda _name: AgentDef(name="build", description="test"))
    monkeypatch.setattr(litellm, "acompletion", no_title)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _model: {})
    monkeypatch.setattr("agent.suggestions.generate_suggestions", AsyncMock(return_value=[]))
    # Timer-driven SQL (the abort poll every 0.5 s, hold observation, streaming
    # checkpoints, lease-check reuse) depends on wall time, not on the turn's
    # work; a slow machine would add statements. Stretch the timers so only the
    # work is counted. Section 13 of the plan estimates the timer-driven part.
    monkeypatch.setattr("agent.driver.ABORT_POLL_SECONDS", 3600.0)
    monkeypatch.setattr("agent.driver.HOLD_OBSERVE_SECONDS", 3600.0)
    monkeypatch.setattr("agent.processor.STREAM_CHECKPOINT_INTERVAL", 3600.0)
    monkeypatch.setattr("question.runtime.VERIFIED_REUSE_SECONDS", 3600.0)
    from trajectory.emitter import Emitter
    enqueue = Emitter._enqueue

    def recorded(self, *args, **kwargs):  # every event and control line the spool receives
        harness.recorded += 1
        return enqueue(self, *args, **kwargs)
    harness.recorded = 0
    monkeypatch.setattr(Emitter, "_enqueue", recorded)
    if scenario == "production":
        async def route(state, memory_config, **kwargs):
            harness.routed.append(state["utterance"])
            sure = {"retrieve": 0.95, "skip": 0.03, "unknown": 0.02}
            return {"model": memory_config.jev_model, "usage": {"input_tokens": 32, "output_tokens": 0},
                    "answers": {"memory_needed": {"choice": "retrieve", "probabilities": sure, "confidence": 0.95},
                                "task_needed": {"choice": "skip", "confidence": 0.95,
                                                "probabilities": {"read": 0.03, "skip": 0.95, "unknown": 0.02}}}}
        monkeypatch.setattr("memory.routing.evaluate_context_needs", route)
    return harness


class EchoArgs(BaseModel):
    text: str = ""


def _text_provider():
    async def provider(*args, **kwargs):
        yield {"type": "text_delta", "text": "好的，已收到。"}
        yield {"type": "finish", "reason": "stop", "usage": {}}
    return provider


def _tool_provider():
    calls = {"n": 0}

    async def provider(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            yield {"type": "tool_call", "tool": "echo", "args": {"text": "hi"}, "call_id": f"call_{calls['n']}"}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            yield {"type": "text_delta", "text": "工具已执行。"}
            yield {"type": "finish", "reason": "stop", "usage": {}}
    return provider


@pytest.mark.parametrize("kind", ["text", "tool"])
@pytest.mark.parametrize("scenario", ["baseline", "production"])
async def test_turn_sql_within_budget(scenario, kind, counted, no_network, monkeypatch):
    from session.session import create_user_message, update_session
    from tool.tool import ToolResult, define_tool

    harness = _harness(monkeypatch, scenario)
    await update_session("s1", user_id="u1", title="budget", model="openai/gpt-4o")
    if kind == "tool":
        async def echo(args, ctx):
            return ToolResult(title="echo", output=args.text or "ok")
        harness.tools["echo"] = define_tool(
            "echo", description="Echo text.", parameters=EchoArgs, execute=echo, sandbox_required=False)
        provider = _tool_provider()
    else:
        provider = _text_provider()
    monkeypatch.setattr(harness.processor, "stream_llm", provider)

    per_turn = []
    dump = os.environ.get("KERNEL_SQL_BUDGET_DUMP")
    for turn in range(1, WARMUP_TURNS + 2):
        await create_user_message("s1", f"第 {turn} 轮：请简单回答。", model="openai/gpt-4o", user_id="u1")
        statements, commits = counted.statements, counted.commits
        assert await harness.loop.run_loop("s1", user_id="u1") is not None
        per_turn.append((counted.statements - statements, counted.commits - commits))
        if dump:
            # One statement per line, for diffing a turn before and after a change.
            path = Path(dump) / f"{scenario}-{kind}-turn{turn}.sql"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(counted.text[statements:counted.statements]) + "\n", encoding="utf-8")
    measured = per_turn[-1]
    budget = BUDGET[(scenario, kind)]
    print(f"\n[kernel SQL budget] {scenario}/{kind}: per turn (statements, commits) = {per_turn}; "
          f"measured {measured}, budget {budget}")
    assert not no_network, f"the offline scenario tried to reach the network: {no_network}"
    # A scenario that silently stopped recalling or recording would pass its budget
    # without measuring it.
    if scenario == "production":
        assert len(harness.routed) == len(per_turn), (
            f"memory recall routed {len(harness.routed)} of {len(per_turn)} turns; the production "
            "scenario no longer reaches recall. Fix the scenario setup before trusting its budget.")
        assert harness.recorded, "trajectory recording emitted nothing; fix the scenario setup."
    else:
        assert not harness.routed and not harness.recorded, "the baseline scenario recalled or recorded"
    assert measured[0] <= budget[0] and measured[1] <= budget[1], (
        f"{scenario} {kind} turn: {measured[0]} SQL statements / {measured[1]} commits, "
        f"budget {budget[0]} / {budget[1]}. The change adds SQL to every turn. To find the new statements, "
        "run this test with KERNEL_SQL_BUDGET_DUMP=<dir> on the change and on its parent and diff "
        f"{scenario}-{kind}-turn{len(per_turn)}.sql. Remove them, or raise BUDGET with the reason in the "
        "change description.")
    if measured != budget:
        print(f"[kernel SQL budget] {scenario}/{kind} is below budget: lower BUDGET to {measured} "
              "in this change.")
