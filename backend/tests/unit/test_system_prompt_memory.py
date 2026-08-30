"""Creator memory injection into the system prompt: last part, gated, resilient."""
from types import SimpleNamespace

import pytest

from agent.agent import AgentDef
from agent.loop import _build_system_prompt


@pytest.mark.asyncio
async def test_environment_prompt_describes_wuying_without_docker_claims(monkeypatch):
    async def no_instructions(_config):
        return []

    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(sandbox_provider="wuying"),
    )
    monkeypatch.setattr(
        "session.instruction.instruction_system_with_config",
        no_instructions,
    )

    parts = await _build_system_prompt(
        AgentDef(name="explore", description="", prompt="x"),
        "openai/gpt-5",
    )
    environment = next(part for part in parts if part.startswith("You are powered"))
    assert "Alibaba Cloud Wuying workstation" in environment
    assert "Docker" not in environment
    assert "Kubernetes" not in environment
    assert "root (full system access)" not in environment


@pytest.mark.asyncio
async def test_memory_appended_last_for_build_agent(monkeypatch):
    async def fake_assemble(*, user_id, project_id=None, volatile_limit=5):
        assert user_id == "user_1"
        return {"context": "## 创作者人设(已知)\n- **身份**: 玉石主播", "stats": {}}

    monkeypatch.setattr("memory.context.assemble_user_context", fake_assemble)
    parts = await _build_system_prompt(
        AgentDef(name="build", description=""), "openai/gpt-5", user_id="user_1"
    )
    assert parts[-1].startswith("<user_memory>")
    assert "玉石主播" in parts[-1]
    assert parts[-1].endswith("</user_memory>")


@pytest.mark.asyncio
async def test_no_memory_part_when_empty_or_wrong_agent(monkeypatch):
    async def fake_assemble(**_kwargs):
        return {"context": "", "stats": {}}

    monkeypatch.setattr("memory.context.assemble_user_context", fake_assemble)
    empty = await _build_system_prompt(
        AgentDef(name="build", description=""), "openai/gpt-5", user_id="user_1"
    )
    assert not any("<user_memory>" in part for part in empty)

    async def should_not_run(**_kwargs):  # pragma: no cover - guard
        raise AssertionError("hidden agents must not assemble memory")

    monkeypatch.setattr("memory.context.assemble_user_context", should_not_run)
    for name in ("title", "compaction", "explore"):
        parts = await _build_system_prompt(
            AgentDef(name=name, description="", prompt="x"), "openai/gpt-5", user_id="user_1"
        )
        assert not any("<user_memory>" in part for part in parts)


@pytest.mark.asyncio
async def test_assembler_failure_never_blocks_the_prompt(monkeypatch):
    async def broken(**_kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setattr("memory.context.assemble_user_context", broken)
    parts = await _build_system_prompt(
        AgentDef(name="build", description=""), "openai/gpt-5", user_id="user_1"
    )
    assert parts  # prompt still builds
    assert not any("<user_memory>" in part for part in parts)
