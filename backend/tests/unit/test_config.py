"""Test new config fields are present and have correct defaults."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import OpenBoxConfig, ToolExposureConfig, _load_json


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
    assert config.image_generation.model == "gpt-image-2"
    assert config.image_generation.default_size == "auto"
    assert config.image_generation.default_quality == "medium"
    assert config.image_generation.output_format == "png"
    assert config.image_generation.timeout_seconds == 600


def test_video_generation_defaults_to_wan_3():
    config = OpenBoxConfig()
    assert config.video_generation.model == "wan3.0-video"
    assert config.video_generation.default_resolution == "1080p"


def test_example_binds_default_wan_3_to_the_bossip_protocol():
    from tool.video_providers import declared_model, resolve_route, validate_request

    path = Path(__file__).parents[2] / "openbox.jsonc.example"
    config = OpenBoxConfig(**_load_json(path))
    settings = config.video_generation
    declared = {model.id: model for model in settings.models}

    assert settings.model == "wan3.0-video"
    assert settings.default_resolution == "1080p"
    assert declared[settings.model].channel == "sd2"
    assert declared[settings.model].provider == "newapi"
    assert declared[settings.model].resolutions == ["1080p"]

    route = resolve_route(None, config)
    validate_request(
        route,
        resolution=settings.default_resolution,
        ratio=settings.default_ratio,
        duration=settings.default_duration,
        generate_audio=settings.default_generate_audio,
        input_mimes=["image/png"],
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


def test_tool_exposure_config_defaults_are_migration_safe():
    config = OpenBoxConfig().tool_exposure
    assert config.mode == "legacy_eager"
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
