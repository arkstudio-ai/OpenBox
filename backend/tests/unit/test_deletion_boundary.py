"""Every interactive model receives the shipped shared deletion boundary."""
from pathlib import Path

import pytest

from agent.agent import AgentDef
from agent.loop import _build_system_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [
    "openai/gemini-3.8-flash", "openai/gpt-5.6-luna", "anthropic/claude",
    "openai/qwen3.8-max", "openai/trinity", "openai/gpt-4",
])
async def test_deletion_boundary_is_loaded_for_each_model(monkeypatch, model):
    backend = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(backend)
    policy = (backend / "AGENTS.md").read_text()
    parts = await _build_system_prompt(AgentDef(name="build", description=""), model)
    assert any(policy in part for part in parts)
