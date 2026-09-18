"""Clients receive the effective compaction ceiling, not a guessed percentage."""
import pytest

from api.metadata import get_config
from core.config import ModelConfig, OpenBoxConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved, expected", [(None, 80000), (50000, 50000)])
async def test_config_advertises_context_and_reasoning_output_ceiling(monkeypatch, reserved, expected):
    cfg = OpenBoxConfig(model="openai/gpt-5", models=[
        ModelConfig(id="openai/gpt-5", context_limit=100000),
    ])
    cfg.compaction.reserved = reserved
    monkeypatch.setattr("core.config.get_config", lambda: cfg)
    monkeypatch.setattr("agent.llm.request_output_tokens",
                        lambda model, variant=None: 64000 if variant == "high" else 8192)
    result = await get_config()
    model = result["models"][0]
    assert model["context_limit"] == 100000
    assert model["compaction"]["enabled"] is True
    assert model["compaction"]["threshold"] == expected
    assert model["compaction"]["variants"]["high"] == 36000


@pytest.mark.asyncio
async def test_config_does_not_advertise_auto_compaction_when_disabled(monkeypatch):
    cfg = OpenBoxConfig(model="openai/gpt-5")
    cfg.compaction.auto = False
    monkeypatch.setattr("core.config.get_config", lambda: cfg)
    result = await get_config()
    assert result["models"][0]["compaction"] == {
        "enabled": False, "threshold": None, "variants": {},
    }
