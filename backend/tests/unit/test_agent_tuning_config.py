"""Rollout switches for the step-1 prompt and skill-loading changes."""

import pytest

import core.config as config_mod
from core.config import AgentTuningConfig, OpenBoxConfig, agent_tuning_enabled


@pytest.fixture
def tuning(monkeypatch):
    def install(**fields):
        cfg = OpenBoxConfig(agent_tuning=AgentTuningConfig(**fields))
        monkeypatch.setattr(config_mod, "_config", cfg)
        return cfg

    return install


def test_every_switch_defaults_off(tuning):
    tuning()
    for name in AgentTuningConfig.model_fields:
        if name == "workspaces":
            continue
        assert agent_tuning_enabled(name, "ws_any") is False


def test_switch_on_for_whole_deployment(tuning):
    tuning(relaxed_prompt=True)
    assert agent_tuning_enabled("relaxed_prompt", "ws_any") is True
    assert agent_tuning_enabled("relaxed_prompt", None) is True
    assert agent_tuning_enabled("tool_name_listing", "ws_any") is False


def test_workspace_list_narrows_enabled_switches(tuning):
    tuning(relaxed_prompt=True, workspaces=["ws_internal"])
    assert agent_tuning_enabled("relaxed_prompt", "ws_internal") is True
    assert agent_tuning_enabled("relaxed_prompt", "ws_customer") is False
    assert agent_tuning_enabled("relaxed_prompt", None) is False
    assert agent_tuning_enabled("relaxed_prompt", "") is False


def test_unknown_switch_fails_loudly(tuning):
    tuning()
    with pytest.raises(KeyError):
        agent_tuning_enabled("relaxed_promt", "ws")
    with pytest.raises(KeyError):
        agent_tuning_enabled("workspaces", "ws")


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("AGENT_TUNING_RELAXED_PROMPT", "true")
    monkeypatch.setenv("AGENT_TUNING_WORKSPACES", "ws_a, ws_b")
    data = config_mod._apply_env_overrides({})
    assert data["agent_tuning"] == {"relaxed_prompt": True, "workspaces": ["ws_a", "ws_b"]}


def test_environment_override_rejects_non_boolean(monkeypatch):
    monkeypatch.setenv("AGENT_TUNING_RELAXED_PROMPT", "yes")
    with pytest.raises(ValueError):
        config_mod._apply_env_overrides({})
