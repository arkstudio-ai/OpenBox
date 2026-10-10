"""Voice settings: VOICE_* overrides, the DASHSCOPE fallback, the endpoint and the client flag."""
import pytest

from api.metadata import get_config
from core.config import OpenBoxConfig, VoiceConfig, _apply_env_overrides
from voice import config as voice_settings


def test_voice_environment_overrides_read_like_the_memory_ones(monkeypatch):
    monkeypatch.setenv("VOICE_ENABLED", "true")
    monkeypatch.setenv("VOICE_VOICE", "Tina")
    monkeypatch.setenv("VOICE_MAX_CALL_SECONDS", "600")
    monkeypatch.setenv("VOICE_WORKSPACE_ID", "llm-abc123")
    monkeypatch.setenv("VOICE_TURN_MODEL", "openai/qwen3.8-flash")
    monkeypatch.setenv("VOICE_TURN_VARIANT", "low")
    config = OpenBoxConfig(**_apply_env_overrides({"voice": {"model": "kept"}}))
    assert (config.voice.enabled, config.voice.voice, config.voice.model) == (True, "Tina", "kept")
    assert config.voice.max_call_seconds == 600
    assert not hasattr(config.voice, "daily_seconds")  # calls are paid in credits, no time quota
    assert (config.voice.turn_model, config.voice.turn_variant) == ("openai/qwen3.8-flash", "low")
    monkeypatch.setenv("VOICE_ENABLED", "yes")
    with pytest.raises(ValueError, match="VOICE_ENABLED must be a boolean"):
        _apply_env_overrides({})


def test_voice_turns_keep_the_main_sessions_model_unless_configured():
    voice = VoiceConfig()
    assert (voice.turn_model, voice.turn_variant, voice.late_after_seconds) == (None, None, 12)
    assert voice.summary_model == "openai/qwen3.8-flash"


def test_audio_config_overrides_and_history_limits(monkeypatch):
    monkeypatch.setenv("VOICE_MODEL", "qwen-audio-3.1-realtime-plus")
    monkeypatch.setenv("VOICE_MAX_HISTORY_TURNS", "40")
    monkeypatch.setenv("VOICE_ENABLE_SPEECH_EMOTION", "false")
    config = OpenBoxConfig(**_apply_env_overrides({})).voice
    assert config.model == "qwen-audio-3.1-realtime-plus"
    assert config.max_history_turns == 40 and not config.enable_speech_emotion
    with pytest.raises(ValueError):
        VoiceConfig(max_history_turns=51)


def test_enabled_needs_a_key_and_falls_back_to_dashscope(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert not voice_settings.enabled(VoiceConfig(enabled=True))
    assert voice_settings.enabled(VoiceConfig(enabled=True, api_key="configured"))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "from-env")
    assert voice_settings.api_key(VoiceConfig()) == "from-env"
    assert voice_settings.enabled(VoiceConfig(enabled=True)) and not voice_settings.enabled(VoiceConfig())


def test_endpoint_business_space_and_proxy_plan():
    assert voice_settings.realtime_url(VoiceConfig()) == (
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3.8-omni-flash-realtime")
    assert voice_settings.realtime_url(VoiceConfig(workspace_id="llm-abc123")).startswith(
        "wss://llm-abc123.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=")
    with pytest.raises(ValueError):
        voice_settings.realtime_url(VoiceConfig(workspace_id="evil.example.com/x"))
    assert voice_settings.proxy_plan(VoiceConfig()) == [True, None, True]  # proxy, direct, proxy
    assert voice_settings.proxy_plan(VoiceConfig(proxy="none", connect_attempts=2)) == [None, None]


async def test_clients_learn_whether_to_show_the_call_button(monkeypatch):
    cfg = OpenBoxConfig(model="openai/gpt-5")
    monkeypatch.setattr("core.config.get_config", lambda: cfg)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert (await get_config())["voice_enabled"] is False
    cfg.voice = VoiceConfig(enabled=True, api_key="configured")
    assert (await get_config())["voice_enabled"] is True
