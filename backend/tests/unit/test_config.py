"""Test new config fields are present and have correct defaults."""
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import OpenBoxConfig, ToolExposureConfig, _find_config_files, _load_json


def test_database_config_defaults():
    config = OpenBoxConfig()
    assert "postgresql+asyncpg" in config.database_url
    assert config.db_pool_size == 10
    assert config.db_pool_overflow == 20


def test_redis_config_defaults():
    config = OpenBoxConfig()
    assert "redis://" in config.redis_url


def test_blob_config_defaults():
    config = OpenBoxConfig()
    assert config.blob_provider == "azure"
    assert config.blob_azure_container == "ads-staging"


def test_auth_config_defaults():
    config = OpenBoxConfig()
    assert config.jwt_secret == ""
    assert config.jwt_access_expire_minutes == 15
    assert config.jwt_refresh_expire_days == 7


def test_quota_config_defaults():
    config = OpenBoxConfig()
    assert config.max_containers_per_user == 5
    assert config.max_sessions_per_user == 200
    assert config.monthly_cost_limit == 50.0


def test_rate_limit_config_defaults():
    config = OpenBoxConfig()
    assert config.rate_limit_login == "5/minute"
    assert config.rate_limit_api == "60/minute"


def test_image_generation_config_defaults():
    config = OpenBoxConfig()
    assert (config.image_generation.provider, config.image_generation.model) == ("runninghub", "seedream-v5-pro")
    assert config.image_generation.default_size == "auto"
    assert config.image_generation.default_quality == "medium"
    assert config.image_generation.output_format == "png"
    assert config.image_generation.timeout_seconds == 600


def test_example_images_use_a_runninghub_model_that_runs_on_the_cn_site():
    """2026-10-10: runninghub.cn answers 40310 for Nano Banana / GPT Image 2."""
    from tool.image_runninghub import MODELS

    config = OpenBoxConfig(**_load_json(Path(__file__).parents[2] / "openbox.jsonc.example"))
    settings = config.image_generation
    assert (settings.provider, settings.model) == ("runninghub", "seedream-v5-pro")
    assert settings.model in MODELS
    assert config.provider["runninghub"].base_url == "https://www.runninghub.cn"


def test_example_offers_no_retired_gpt_model_or_background_route():
    config = OpenBoxConfig(**_load_json(Path(__file__).parents[2] / "openbox.jsonc.example"))
    models = [config.model, config.mcp_filter_model, config.image_generation.model,
              config.asset_description.model, config.voice.model, config.voice.summary_model,
              config.voice.turn_model or "", *[row.id for row in config.models]]
    assert not any("gpt" in model.lower() for model in models)
    assert config.asset_description.model in {row.id for row in config.models}


def test_video_generation_defaults_to_wan_3_at_720p():
    """720p is the tier every model offers and what a vertical short needs.

    Defaulting to 1080p quietly made every generation the most expensive one
    on the menu, for a picture nobody had asked to be that large.
    """
    config = OpenBoxConfig()
    assert config.video_generation.model == "wan3.0-video"
    assert config.video_generation.default_resolution == "720p"


def test_example_defaults_to_runninghub_and_preserves_wan_3_protocol():
    from tool.video_providers import declared_model, resolve_route, validate_request

    path = Path(__file__).parents[2] / "openbox.jsonc.example"
    config = OpenBoxConfig(**_load_json(path))
    settings = config.video_generation
    declared = {model.id: model for model in settings.models}

    assert settings.model == "MiniMax-H3-Max-Turbo"
    assert settings.default_resolution == "768p"
    assert settings.default_duration == 5
    assert declared[settings.model].channel == "runninghub"
    assert declared[settings.model].provider == "runninghub"
    # Measured 2026-09-01: wan3 honours all three tiers once the request
    # carries them under `metadata`, which is where its adaptor reads.
    assert declared['wan3.0-video'].resolutions == ["480p", "720p", "1080p"]
    assert declared['wan3.0-video'].channel == 'sd2'
    assert declared['wan3.0-video'].provider == 'newapi'
    assert settings.default_resolution in declared[settings.model].resolutions

    route = resolve_route(None, config)
    validate_request(
        route,
        resolution=settings.default_resolution,
        ratio=settings.default_ratio,
        duration=settings.default_duration,
        generate_audio=settings.default_generate_audio,
        input_mimes=[],
        declared=declared_model(settings.model, config),
    )


def test_video_transcription_config_defaults():
    config = OpenBoxConfig()
    assert config.video_transcription.engine == "dashscope"
    assert config.video_transcription.base_url == "https://dashscope.aliyuncs.com"
    assert config.video_transcription.model == "fun-asr"
    assert config.video_transcription.timeout_seconds == 180
    assert config.video_transcription.poll_interval_seconds == 1.0
    assert config.video_transcription.similarity_threshold == 0.90


def test_tool_exposure_defaults_to_portable_with_explicit_legacy_rollback():
    config = OpenBoxConfig().tool_exposure
    assert config.mode == "portable"
    assert ToolExposureConfig(mode="legacy_eager").mode == "legacy_eager"
    assert config.resident_hard_chars == 24_000
    assert config.active_hard_chars == 32_000
    assert config.native_wire_hard_chars == 128_000
    assert config.reveal_ttl_seconds == 1_800
    assert config.max_persisted_reveals == 8
    assert config.max_search_calls_per_step == 2
    assert config.max_reveals_per_step == 5
    assert config.max_search_result_chars_per_step == 2_000
    assert config.allow_emergency_eager is False


def test_tool_exposure_soft_budget_cannot_exceed_hard_budget():
    with pytest.raises(ValidationError, match="resident_soft_chars"):
        ToolExposureConfig(resident_soft_chars=25_000, resident_hard_chars=24_000)


def test_emergency_eager_requires_the_separate_safety_switch():
    with pytest.raises(ValidationError, match="allow_emergency_eager"):
        ToolExposureConfig(mode="emergency_eager")

    config = ToolExposureConfig(
        mode="emergency_eager",
        allow_emergency_eager=True,
    )
    assert config.mode == "emergency_eager"


def test_native_wire_ceiling_cannot_be_configured_above_128k():
    with pytest.raises(ValidationError, match="less than or equal to 128000"):
        ToolExposureConfig(native_wire_hard_chars=128_001)


# --- the test session reads no private config (tests/conftest.py) ---------------------------------

BACKEND = Path(__file__).resolve().parents[2]
INLINE = {"OPENBOX_CONFIG", "OPENAGENT_CONFIG", "OPENBOX_CONFIG_CONTENT", "OPENAGENT_CONFIG_CONTENT"}


def test_the_test_session_finds_no_config_file_and_no_inline_config(monkeypatch):
    """Whatever the developer keeps in backend/openbox.json, ~/.config/openbox or the shell."""
    assert _find_config_files() == []
    monkeypatch.chdir(BACKEND)  # where a private backend/openbox.json would be found
    assert _find_config_files() == []
    assert not INLINE & set(os.environ)


def test_discovery_stays_off_even_where_a_private_file_exists(tmp_path, monkeypatch):
    (tmp_path / "xdg" / "openbox").mkdir(parents=True)
    (tmp_path / "xdg" / "openbox" / "openbox.json").write_text('{"suggestion_model": "openai/private"}')
    (tmp_path / "openbox.json").write_text('{"suggestion_model": "openai/private"}')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    assert _find_config_files() == []
    explicit = tmp_path / "explicit.json"
    explicit.write_text("{}")
    monkeypatch.setenv("OPENBOX_CONFIG", str(explicit))
    assert _find_config_files() == [explicit]  # a test that points at a file has set it up


def test_a_subprocess_started_by_a_test_finds_no_config_file_either():
    script = "from core.config import _find_config_files; print([str(p) for p in _find_config_files()])"
    result = subprocess.run([sys.executable, "-c", script], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "[]"


def test_dotenv_files_are_not_loaded_into_the_test_process():
    """main.py and litellm (in its DEV mode) would otherwise load backend/.env on import."""
    from dotenv import load_dotenv
    assert os.environ["LITELLM_MODE"] == "PRODUCTION"
    assert load_dotenv(stream=io.StringIO("OPENBOX_DOTENV_CANARY=1\n")) is False
    assert "OPENBOX_DOTENV_CANARY" not in os.environ


def test_a_test_can_opt_in_to_file_based_config(config_files):
    import core.config
    config_files.global_file.write_text('{"suggestion_model": "openai/global", "instructions": ["G.md"]}')
    config_files.project_file.write_text('{"suggestion_model": "openai/project"}')
    assert _find_config_files() == [config_files.global_file, config_files.project_file]
    config = core.config.get_config()
    assert (config.suggestion_model, config.instructions) == ("openai/project", ["G.md"])
