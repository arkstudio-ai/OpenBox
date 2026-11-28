"""Long-term memory as kernel plugins (docs/KERNEL_DSH_COMPARISON_20261128.md 9.1, stage one).

The entry point registers ``MemoryCleanup`` and ``MemoryPlugin``. Without the
feature plugin the app starts and a chat turn runs, carrying no memory prompt
section or tool, while the cleanup obligations keep running: the index and
document workers start, and memory text is still stripped from history.
"""
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

import main
from agent import extensions as ext
from core.config import MemoryConfig, OpenBoxConfig
from memory.plugin import MemoryCleanup, MemoryPlugin
from tool.tool import ToolResult, define_tool
from trajectory.plugin import TrajectoryPlugin
from tests.unit.test_main_trajectory_lifecycle import quiet_backend  # noqa: F401
from tests.unit.test_run_fencing_api import loop_harness, state, statements  # noqa: F401

MEMORY_TOOLS = {"memory_prepare", "memory_search", "memory_read_sources", "memory_read_more",
                "current_task_state", "memory_forget"}
MEMORY_TAGS = ("<memory_usage>", "<memory_saving>", "<user_memory>")
MEMORY_ROUTES = ("/api/memories", "/api/memory-")


def with_memory():
    return ext.KernelExtensions().add(TrajectoryPlugin()).add(MemoryCleanup()).add(MemoryPlugin())


def without_memory_feature():
    """What an entry point registers with the memory feature left out: the cleanup obligations stay."""
    return ext.KernelExtensions().add(TrajectoryPlugin()).add(MemoryCleanup())


@pytest.fixture(autouse=True)
async def fresh_database_afterwards():
    """The lifespan tests open the shared test engine inside their own event loop."""
    yield
    from db.base import close_engine
    await close_engine()


def test_the_entry_point_registers_memory_cleanup_and_the_feature_plugin():
    registry = main._build_kernel_extensions(None)
    assert [plugin.name for plugin in registry.plugins] == ["trajectory", "memory.cleanup", "memory"]
    assert [(section.name, section.order) for section in registry.prompt_sections] == [
        ("memory.use_guidance", 400), ("memory.automatic_saving", 410), ("memory.user_context", 900)]
    assert all(section.order < ext.PROMPT_ORDER_PROJECT_BRIEF for section in registry.prompt_sections[:2])
    assert [pack.name for pack in registry.tool_packs] == ["memory"]
    assert set(registry.tool_packs[0].tools()) == MEMORY_TOOLS
    assert [hook.name for hook in registry.recovery_hooks] == ["memory.extraction_jobs"]


async def test_without_the_feature_plugin_prompts_and_tools_carry_no_memory(monkeypatch):
    from agent.agent import get_agent
    from agent.loop import _build_system_prompt
    from agent.tool_resolution import resolve_step_tools
    from tool import registry

    config = OpenBoxConfig(memory=MemoryConfig(automatic_knowledge=True, auto_extract=True, v2_write=True,
                                               retrieval_v2=True))
    monkeypatch.setattr("core.config.get_config", lambda: config)

    async def assembled(**_kwargs):
        return {"context": "## 创作者人设(已知)", "stats": {}}
    monkeypatch.setattr("memory.context.assemble_user_context", assembled)
    with ext.use(with_memory()):
        full = await _build_system_prompt(get_agent("build"), "openai/gpt-5", user_id="u1")
    with ext.use(without_memory_feature()):
        bare = await _build_system_prompt(get_agent("build"), "openai/gpt-5", user_id="u1")
    assert all(any(tag in part for part in full) for tag in MEMORY_TAGS) and full[-1].startswith("<user_memory>")
    assert not any(tag in part for part in bare for tag in MEMORY_TAGS)
    assert [part for part in full if not any(tag in part for tag in MEMORY_TAGS)] == bare

    for registered, expected in ((with_memory, MEMORY_TOOLS - {"memory_prepare"}), (without_memory_feature, set())):
        monkeypatch.setattr(registry, "_tools", {})
        with ext.use(registered()):
            registry.register_builtin_tools(load_custom=False)
            resolved = await resolve_step_tools(get_agent("build"), None, [], include_discovery=False)
        assert MEMORY_TOOLS & set(resolved) == expected
        assert MEMORY_TOOLS & set(registry._tools) == (MEMORY_TOOLS if expected else set())


async def test_without_the_feature_plugin_a_chat_turn_runs(state, loop_harness, statements, monkeypatch):
    from session.session import create_user_message, update_session

    class EchoArgs(BaseModel):
        text: str = ""

    async def echo(args, ctx):
        return ToolResult(title="echo", output=args.text)

    loop_harness.tools["echo"] = define_tool("echo", description="Echo text.", parameters=EchoArgs,
                                             execute=echo, sandbox_required=False)
    calls = {"n": 0}

    async def provider(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            yield {"type": "tool_call", "tool": "echo", "args": {"text": "hi"}, "call_id": f"call_{calls['n']}"}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "finish", "reason": "stop", "usage": {}}
    monkeypatch.setattr(loop_harness.processor, "stream_llm", provider)
    monkeypatch.setattr("agent.suggestions.generate_suggestions", AsyncMock(return_value=[]))
    await update_session("s1", user_id="u1", title="bench", model="openai/gpt-4o")

    async def turn(number):
        await create_user_message("s1", f"turn {number}", model="openai/gpt-4o", user_id="u1")
        before = len(statements)
        assert await loop_harness.loop.run_loop("s1", user_id="u1") is not None
        return len(statements) - before

    await turn(1)
    with_plugin = await turn(2)
    with ext.use(without_memory_feature()):
        without_plugin = await turn(3)
    assert without_plugin <= with_plugin


@pytest.mark.parametrize("registered,workers,mounted", [
    (with_memory, ["index", "documents", "extraction", "wiki", "organization"], True),
    (without_memory_feature, ["index", "documents"], False),
])
async def test_the_app_starts_and_stops_with_or_without_the_feature_plugin(
        quiet_backend, monkeypatch, registered, workers, mounted):
    from memory.documents.worker import MemoryDocumentWorker
    from memory.extraction import MemoryExtractionWorker
    from memory.outbox import MemoryIndexWorker
    from memory.wiki.organization_worker import WikiOrganizationWorker
    from memory.wiki.worker import MemoryWikiWorker

    started, stopped = [], []
    for worker, label in ((MemoryIndexWorker, "index"), (MemoryDocumentWorker, "documents"),
                          (MemoryWikiWorker, "wiki"), (WikiOrganizationWorker, "organization")):
        monkeypatch.setattr(worker, "start", lambda self, label=label: started.append(label))
        monkeypatch.setattr(worker, "stop", AsyncMock(side_effect=lambda label=label: stopped.append(label)))
    monkeypatch.setattr(MemoryExtractionWorker, "start", AsyncMock(side_effect=lambda: started.append("extraction")))
    monkeypatch.setattr(MemoryExtractionWorker, "stop", AsyncMock(side_effect=lambda: stopped.append("extraction")))
    pool_closed = AsyncMock()
    monkeypatch.setattr("memory.providers.common.close_shared_clients", pool_closed)
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(jwt_secret=""))
    monkeypatch.setenv("TRAJECTORY_WORKER_MODE", "off")
    monkeypatch.setattr(main, "_build_kernel_extensions", lambda config: registered())

    app = main.create_app()
    assert any(route.path.startswith(MEMORY_ROUTES) for route in app.routes) is mounted
    async with main.lifespan(app):
        assert started == workers and stopped == []
    assert sorted(stopped) == sorted(workers)
    # The pooled provider clients close after every worker stopped, with or without the feature.
    pool_closed.assert_awaited_once()


def test_memory_text_is_stripped_from_history_without_the_feature_plugin():
    from session.agent_event_log import strip_memory_text
    references = [{"kind": "memory", "id": "m1", "revision": 1}]
    part = {"type": "tool", "tool": "memory_search", "output": "用户喜欢简短中文回复",
            "metadata": {"transient_memory_refs": {"references": references}}}
    with ext.use(without_memory_feature()):
        stripped = strip_memory_text(part)
    assert "用户喜欢" not in stripped["output"]
    assert json.loads(stripped["output"])["references"] == references


async def test_extraction_job_recovery_is_a_recovery_hook_that_never_raises(monkeypatch):
    calls = []

    async def recover(*, limit):
        calls.append(limit)
        return 2
    monkeypatch.setattr("memory.jobs.recover_extraction_jobs", recover)
    with ext.use(with_memory()):
        assert await ext.run_recovery_hooks() == 2
    with ext.use(without_memory_feature()):
        assert await ext.run_recovery_hooks() == 0
    assert calls == [100]

    async def broken(*, limit):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr("memory.jobs.recover_extraction_jobs", broken)
    with ext.use(with_memory()):
        assert await ext.run_recovery_hooks() == 0
