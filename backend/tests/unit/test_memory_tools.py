"""Supplemental tools enforce SQL scope, admission, and live business state."""
from datetime import datetime, timezone
import json
from uuid import uuid4

from pydantic import ValidationError
import pytest
from sqlalchemy import select, update

from core import config as config_module
from db.base import get_db_session
from db.models.memory_v2 import MemorySource, MemorySourceLink
from db.models.session import Session
from db.models.todo import Todo
from memory import service
from memory.policy import resolve_access_scope
from memory.providers.common import MemoryProviderError
from tests.unit.test_memory_pipeline import _seed, pipeline_database  # noqa: F401
from tool import memory_tools as tools
from tool.tool import ToolContext


async def seed_tools(monkeypatch):
    seed = await _seed(monkeypatch)
    config = config_module.get_config()
    config.memory.auto_extract = False
    config.memory.retrieval_v2 = True
    config.memory.debug_view = False
    config.memory.rerank = False

    async def unavailable_embedding(*_args, **_kwargs):
        raise MemoryProviderError("embedding_test_unavailable")

    monkeypatch.setattr("memory.retrieval.BailianEmbedding.embed", unavailable_embedding)
    return seed


def tool_context(seed):
    return ToolContext(user_id=seed[0], workspace_id=seed[1], project_id=seed[2], session_id=seed[3])


async def manual_memory(seed, summary="用户喜欢简短中文回复"):
    note = await service.create_note(user_id=seed[0], workspace_id=seed[1], project_id=seed[2], summary=summary)
    async with get_db_session() as db:
        source = await db.scalar(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).where(MemorySourceLink.memory_id == note["id"]))
    return note, source


@pytest.mark.parametrize("model,args", [
    (tools.MemorySearchArgs, {"query": "preference", "user_id": "forged"}),
    (tools.MemoryReadSourcesArgs, {"sources": [{"source_id": "a", "revision": 1}], "workspace_id": "forged"}),
    (tools.CurrentTaskStateArgs, {"project_id": "forged"}),
])
def test_model_cannot_supply_actor_scope(model, args):
    with pytest.raises(ValidationError):
        model.model_validate(args)


async def test_execution_session_rejects_forged_project_or_owner(monkeypatch):
    seed = await seed_tools(monkeypatch)
    await manual_memory(seed)
    ctx = tool_context(seed)
    ctx.project_id = "forged-project"
    result = await tools.execute_memory_search(tools.MemorySearchArgs(query="中文"), ctx)
    assert json.loads(result.output)["status"] == "unavailable"
    ctx.project_id, ctx.user_id = seed[2], "forged-user"
    result = await tools.execute_current_task_state(tools.CurrentTaskStateArgs(), ctx)
    assert json.loads(result.output)["items"] == []


async def test_keyword_fallback_excludes_candidates_and_their_sources(monkeypatch):
    seed = await seed_tools(monkeypatch)
    active, _ = await manual_memory(seed)
    async with get_db_session() as db:
        access = await resolve_access_scope(db, user_id=seed[0], workspace_id=seed[1], project_id=seed[2])
        candidate = await service.create_candidate_in_session(db, access=access, type="PREFERENCE",
            summary="未经确认：用户喜欢中文中的神秘标记", sources=[{"source_kind": "user_statement", "body": "神秘标记仅供候选"}])
        candidate_id = candidate.id
        await db.flush()
        candidate_source = await db.scalar(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).where(MemorySourceLink.memory_id == candidate_id))
    result = await tools.execute_memory_search(tools.MemorySearchArgs(query="中文"), tool_context(seed))
    payload = json.loads(result.output)
    assert payload["status"] == "ok" and payload["untrusted_data"] is True
    assert active["id"] in {item["id"] for item in payload["items"]}
    assert candidate_id not in result.output and "神秘标记" not in result.output
    assert "embedding_test_unavailable" in payload["degraded_reasons"]
    denied = await tools.execute_memory_read_sources(tools.MemoryReadSourcesArgs(
        sources=[{"source_id": candidate_source.id, "revision": candidate_source.source_revision}]), tool_context(seed))
    assert json.loads(denied.output)["items"][0]["available"] is False


async def test_source_idor_exact_revision_and_secret_redaction(monkeypatch):
    seed = await seed_tools(monkeypatch)
    secret = "supplement-only-test-secret-0192"
    monkeypatch.setenv("MEMORY_BAILIAN_API_KEY", secret)
    _note, source = await manual_memory(seed, f"用户的中文偏好；password={secret}；联系 reader@example.test")
    other = await seed_tools(monkeypatch)
    _foreign_note, foreign = await manual_memory(other, "只属于另一个用户的独有内容")
    config_module.get_config().memory.allowed_user_ids = [seed[0], other[0]]
    result = await tools.execute_memory_read_sources(tools.MemoryReadSourcesArgs(sources=[
        {"source_id": source.id, "revision": source.source_revision},
        {"source_id": source.id, "revision": source.source_revision + 1},
        {"source_id": foreign.id, "revision": foreign.source_revision},
    ]), tool_context(seed))
    items = json.loads(result.output)["items"]
    assert items[0]["available"] and items[0]["redacted"]
    assert secret not in result.output and "reader@example.test" not in result.output
    assert items[1]["reason_code"] == "version_changed"
    assert items[2]["available"] is False and items[2]["reason_code"] == "unavailable"
    assert "只属于另一个用户" not in result.output


async def test_current_task_state_uses_business_sql_and_filters_foreign_session(monkeypatch):
    seed = await seed_tools(monkeypatch)
    await manual_memory(seed, "这个任务昨天已经完成")
    async with get_db_session() as db:
        await db.execute(update(Session).where(Session.id == seed[3]).values(status="running"))
        db.add(Todo(id=f"todo-{uuid4().hex}", session_id=seed[3], user_id=seed[0],
                    items=[{"id": "pending", "content": "仍需进行验证", "status": "in_progress"}],
                    updated_at=datetime.now(timezone.utc)))
    result = await tools.execute_current_task_state(tools.CurrentTaskStateArgs(), tool_context(seed))
    payload = json.loads(result.output)
    assert payload["source"] == "business_sql" and payload["observed_at"]
    assert payload["sessions"][0]["status"] == "running"
    assert payload["sessions"][0]["todos"][0]["status"] == "in_progress"
    assert "昨天已经完成" not in result.output
    foreign = await seed_tools(monkeypatch)
    config_module.get_config().memory.allowed_user_ids = [seed[0], foreign[0]]
    result = await tools.execute_current_task_state(tools.CurrentTaskStateArgs(session_id=foreign[3]), tool_context(seed))
    assert json.loads(result.output)["sessions"] == []


async def test_memory_tools_are_resident_and_respect_explicit_allowlists(monkeypatch):
    from agent.agent import AgentDef, get_agent
    from agent.tool_exposure import build_eligible_catalog, portable_plan
    from agent.tool_resolution import resolve_step_tools
    from tool import registry

    seed = await seed_tools(monkeypatch)
    definitions = {tool.id: tool for tool in (tools.memory_search_tool, tools.memory_read_sources_tool, tools.current_task_state_tool)}
    monkeypatch.setattr(registry, "_tools", definitions)
    resolved = await resolve_step_tools(get_agent("build"), None, [], user_id=seed[0], project_id=seed[2])
    assert tools.MEMORY_TOOL_IDS <= resolved.keys()
    plan = portable_plan(build_eligible_catalog(resolved), agent_name="build")
    assert tools.MEMORY_TOOL_IDS <= set(plan.direct_ids)
    custom = AgentDef(name="custom", description="Only exact evidence read", tools=["memory_read_sources"])
    resolved = await resolve_step_tools(custom, None, [], user_id=seed[0], project_id=seed[2])
    assert set(resolved) == {"memory_read_sources"}
    assert set(portable_plan(build_eligible_catalog(resolved), agent_name="custom").direct_ids) == {"memory_read_sources"}
    config_module.get_config().memory.retrieval_v2 = False
    assert await resolve_step_tools(custom, None, [], user_id=seed[0], project_id=seed[2]) == {}
    result = await tools.execute_memory_search(tools.MemorySearchArgs(query="中文"), tool_context(seed))
    assert json.loads(result.output)["reason_code"] == "feature_disabled"


async def test_authority_outage_is_explicit_and_never_echoes_sql_error(monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    seed = await seed_tools(monkeypatch)
    _, source = await manual_memory(seed)
    async def fail_read(*_args, **_kwargs):
        raise SQLAlchemyError("sensitive SQL connection detail")
    monkeypatch.setattr(service, "read_source_in_scope", fail_read)
    result = await tools.execute_memory_read_sources(tools.MemoryReadSourcesArgs(
        sources=[{"source_id": source.id, "revision": source.source_revision}]), tool_context(seed))
    assert json.loads(result.output)["reason_code"] == "authority_unavailable"
    assert "sensitive SQL" not in result.output and "用户喜欢" not in result.output
