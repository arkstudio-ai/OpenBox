"""Every interactive model receives the shipped shared deletion boundary."""
from pathlib import Path

import pytest

from agent.agent import AgentDef
from agent.loop import _build_system_prompt
from core.config import get_config


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [
    "openai/gemini-3.8-flash", "openai/gpt-5.6-luna", "anthropic/claude",
    "openai/qwen3.8-max", "openai/trinity", "openai/gpt-4",
])
async def test_deletion_boundary_is_loaded_for_each_model(monkeypatch, model):
    backend = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(backend)
    policy = (backend / "AGENTS.md").read_text()
    # Since ebe2278c the server checkout's own AGENTS.md is not discovered
    # implicitly; the deployment lists it in ``instructions``. Supply that
    # here instead of depending on a private openbox.json.
    monkeypatch.setattr(get_config(), "instructions", ["AGENTS.md"])
    parts = await _build_system_prompt(AgentDef(name="build", description=""), model)
    assert any(policy in part for part in parts)


@pytest.mark.asyncio
async def test_the_server_checkout_instructions_are_not_discovered_implicitly(monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(backend)
    monkeypatch.setattr(get_config(), "instructions", [])
    policy = (backend / "AGENTS.md").read_text()
    parts = await _build_system_prompt(AgentDef(name="build", description=""), "openai/gpt-5.6-luna")
    assert not any(policy in part for part in parts)
