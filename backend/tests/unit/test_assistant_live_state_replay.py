"""Live control state survives wire serialization and historical read budgets."""
import json

import pytest

from assistant.projection import current_observation
from core.config import get_config
from db.base import get_db_session
from db.models.assistant import AssistantTask
from db.models.workspace import WorkspaceMember
from memory.packet import ReadBudget, TurnContext
from tests.unit.test_assistant_reads import call_tool, read_turn
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tool.assistant_tools import read_operation


@pytest.mark.parametrize("include_tools", [None, False, True])
async def test_history_replay_uses_the_executed_schema_defaults(monkeypatch, include_tools):
    """Raw provider arguments omit defaults; replay must match execution."""
    ctx, lease, _, accepted, report = await read_turn()
    try:
        from datetime import datetime, timezone
        from core.identifier import ascending
        from db.models.part import Part
        async with get_db_session() as db:
            db.add(Part(id=ascending("part"), message_id=report.id,
                session_id=accepted["execution_session_id"], user_id=ctx.user_id,
                type="tool", canonical_tool_id="read", created_at=datetime.now(timezone.utc),
                data={"type": "tool", "tool": "read", "status": "completed", "output": "Technical detail"}))
        monkeypatch.setattr(get_config().memory, "semantic_recall", True)
        monkeypatch.setattr(get_config(), "jwt_secret", "assistant-cursor-test-only")
        ctx.memory_turn = TurnContext(ctx.user_id, ctx.workspace_id, "继续原来的方案",
            session_id=ctx.session_id, entry="assistant", include_all_projects=True, read_policy="allowed")
        ctx.memory_read_budget = ReadBudget()
        args = {"session_id": accepted["execution_session_id"]}
        if include_tools is not None:
            args["include_tools"] = include_tools
        output, ctx, part = await call_tool(ctx, "history.read", args)
        assert not output.metadata.get("error"), output.output
        read = json.loads(output.output)
        assert any(item["type"] == "tool" for item in read["items"]) is (include_tools is True)
        before = (ctx.memory_read_budget.tokens, ctx.memory_read_budget.legacy_reads)
        for _ in range(2):
            assert await current_observation(part.model_dump(), ctx)
        assert (ctx.memory_read_budget.tokens, ctx.memory_read_budget.legacy_reads) == before
        from sqlalchemy import select
        async with get_db_session() as db:
            original = await db.scalar(select(Part).where(Part.message_id == report.id, Part.type == "text"))
            original.data = {**original.data, "text": "Changed source after the read"}
        assert not await current_observation(part.model_dump(), ctx)
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["none", "revision", "membership"])
async def test_task_wire_dates_replay_only_while_current_authority_matches(monkeypatch, change):
    ctx, lease, _, accepted, _ = await read_turn()
    try:
        config = get_config().memory
        monkeypatch.setattr(config, "semantic_recall", True)
        ctx.memory_turn = TurnContext(ctx.user_id, ctx.workspace_id, "继续刚才的任务",
            session_id=ctx.session_id, entry="assistant", include_all_projects=True, read_policy="allowed")
        ctx.memory_read_budget = ReadBudget(tokens=config.read_more_token_budget)
        args = {"task_id": accepted["task_id"]}
        value = await read_operation("tasks.get", args, ctx)
        assert value["task"]["control_revision"] >= 1
        assert ctx.memory_read_budget.tokens == config.read_more_token_budget
        part = {"type": "tool", "tool": "tasks.get", "input": args,
                "output": json.dumps(value, default=str, ensure_ascii=False)}
        async with get_db_session() as db:
            if change == "revision":
                (await db.get(AssistantTask, accepted["task_id"])).control_revision += 1
            elif change == "membership":
                from sqlalchemy import select
                row = await db.scalar(select(WorkspaceMember).where(
                    WorkspaceMember.user_id == ctx.user_id, WorkspaceMember.workspace_id == ctx.workspace_id))
                row.status = "inactive"
        assert await current_observation(part, ctx) is (change == "none")
    finally:
        await lease.release(session_status="idle")


async def test_live_state_budget_does_not_grant_restricted_history(monkeypatch):
    ctx, lease, _, accepted, _ = await read_turn()
    try:
        config = get_config().memory
        monkeypatch.setattr(config, "semantic_recall", True)
        ctx.memory_turn = TurnContext(ctx.user_id, ctx.workspace_id, "别看历史，只看任务进度",
            session_id=ctx.session_id, entry="assistant", include_all_projects=True, read_policy="no_history")
        ctx.memory_read_budget = ReadBudget(tokens=config.read_more_token_budget)
        assert "task" in await read_operation("tasks.get", {"task_id": accepted["task_id"]}, ctx)
        assert (await read_operation("history.read", {"session_id": accepted["execution_session_id"]}, ctx))["status"] == "restricted"
        ctx.memory_turn = TurnContext(ctx.user_id, ctx.workspace_id, "只看这句话", read_policy="current_only")
        assert (await read_operation("tasks.get", {"task_id": accepted["task_id"]}, ctx))["status"] == "restricted"
    finally:
        await lease.release(session_status="idle")


async def test_budget_trimmed_list_replays_its_exact_current_prefix(monkeypatch):
    ctx, lease, _, accepted, _ = await read_turn()
    try:
        monkeypatch.setattr(get_config().memory, "semantic_recall", True)
        ctx.memory_turn = TurnContext(ctx.user_id, ctx.workspace_id, "看看任务",
            session_id=ctx.session_id, entry="assistant", include_all_projects=True, read_policy="allowed")
        args = {"limit": 10}
        value = await read_operation("tasks.list", args, ctx, record=False)
        assert value["items"]
        value.update(items=value["items"][:1], partial=True, omitted_reason="evidence_budget")
        part = {"type": "tool", "tool": "tasks.list", "input": args,
                "output": json.dumps(value, default=str, ensure_ascii=False)}
        assert await current_observation(part, ctx)
        async with get_db_session() as db:
            (await db.get(AssistantTask, accepted["task_id"])).title = "Changed after the observation"
        assert not await current_observation(part, ctx)
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("operation,arguments", [
    ("projects.list", {}), ("sessions.list", {"include_context": True}), ("assets.list", {}),
])
async def test_discovery_preserves_budget_to_read_the_selected_source(monkeypatch, operation, arguments):
    ctx, lease, _, accepted, _ = await read_turn()
    try:
        config = get_config().memory
        monkeypatch.setattr(config, "semantic_recall", True)
        ctx.memory_turn = TurnContext(ctx.user_id, ctx.workspace_id, "继续原来的视频，用照片改方案",
            session_id=ctx.session_id, entry="assistant", include_all_projects=True, read_policy="allowed")
        ctx.memory_read_budget = ReadBudget()
        for _ in range(3):
            result = await read_operation(operation, arguments, ctx)
            assert result.get("status") != "budget_exhausted"
        assert ctx.memory_read_budget.tokens == 0
        output, ctx, _ = await call_tool(ctx, "history.read", {"session_id": accepted["execution_session_id"]})
        result = json.loads(output.output)
        assert result["items"] and ctx.memory_read_budget.tokens > 0
        assert ctx.memory_read_budget.tokens <= config.read_more_token_budget
        ctx.memory_turn = TurnContext(ctx.user_id, ctx.workspace_id, "只看现在这句", read_policy="current_only")
        assert (await read_operation(operation, arguments, ctx))["status"] == "restricted"
    finally:
        await lease.release(session_status="idle")
