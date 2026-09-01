"""Test new config fields are present and have correct defaults."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import OpenBoxConfig, ToolExposureConfig, _apply_env_overrides, _load_json


def test_database_config_defaults():
    config = OpenBoxConfig()
    assert "postgresql+asyncpg" in config.database_url
    assert config.db_pool_size == 10
    assert config.db_pool_overflow == 20


def test_redis_config_defaults():
    config = OpenBoxConfig()
    assert "redis://" in config.redis_url


def test_agent_execution_defaults_to_wuying_only():
    config = OpenBoxConfig()
    assert config.sandbox_provider == "wuying"
    assert "sandbox_image" not in OpenBoxConfig.model_fields
    assert "k8s_namespace" not in OpenBoxConfig.model_fields

    with pytest.raises(ValidationError, match="sandbox_provider"):
        OpenBoxConfig(sandbox_provider="docker")

    backend_root = Path(__file__).resolve().parents[2]
    assert not (backend_root / "sandbox" / "docker.py").exists()
    assert not (backend_root / "sandbox" / "kubernetes.py").exists()


def test_platform_plugin_watch_interval_is_bounded_and_env_configurable(monkeypatch):
    assert OpenBoxConfig().platform_plugin_watch_interval_seconds == 5.0
    with pytest.raises(ValidationError, match="platform_plugin_watch_interval_seconds"):
        OpenBoxConfig(platform_plugin_watch_interval_seconds=0.1)

    monkeypatch.setenv("PLATFORM_PLUGIN_WATCH_INTERVAL_SECONDS", "12.5")
    data = _apply_env_overrides({})
    assert data["platform_plugin_watch_interval_seconds"] == 12.5


def test_blob_config_defaults():
    config = OpenBoxConfig()
    assert config.blob_provider == "azure"
    assert config.blob_azure_container == "ads-staging"


def test_auth_config_defaults():
    config = OpenBoxConfig()
    assert config.jwt_secret == ""
    assert config.jwt_access_expire_minutes == 15
    assert config.jwt_refresh_expire_days == 7


def test_preview_origin_defaults_to_safe_same_origin_fallback():
    config = OpenBoxConfig()
    assert config.preview_public_origin == ""


def test_dedicated_preview_origin_requires_distinct_exact_https_origin():
    config = OpenBoxConfig(
        preview_public_origin="https://preview.example.test:443/",
        cors_origins=["https://app.example.test:443/"],
        control_public_origins=["https://api.example.test:443/"],
    )
    assert config.preview_public_origin == "https://preview.example.test"
    assert config.cors_origins == ["https://app.example.test"]
    assert config.control_public_origins == ["https://api.example.test"]

    with pytest.raises(ValidationError, match="must use https"):
        OpenBoxConfig(
            preview_public_origin="http://preview.example.test",
            cors_origins=["https://app.example.test"],
            control_public_origins=["https://api.example.test"],
        )

    with pytest.raises(ValidationError, match="hostname distinct"):
        OpenBoxConfig(
            preview_public_origin="https://app.example.test:9443",
            cors_origins=["https://app.example.test"],
            control_public_origins=["https://api.example.test"],
        )

    with pytest.raises(ValidationError, match="exact cors_origins"):
        OpenBoxConfig(
            preview_public_origin="https://preview.example.test",
            cors_origins=["*"],
            control_public_origins=["https://api.example.test"],
        )

    with pytest.raises(ValidationError, match="at least one"):
        OpenBoxConfig(
            preview_public_origin="https://preview.example.test",
            cors_origins=[],
            control_public_origins=["https://api.example.test"],
        )

    with pytest.raises(ValidationError, match="explicit control_public_origins"):
        OpenBoxConfig(
            preview_public_origin="https://preview.example.test",
            cors_origins=["https://app.example.test"],
        )


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


def test_tool_exposure_config_defaults_to_bounded_portable_runtime():
    config = OpenBoxConfig().tool_exposure
    assert config.mode == "portable"
    assert config.resident_hard_chars == 24_000
    assert config.active_hard_chars == 32_000
    assert config.native_wire_hard_chars == 128_000
    assert config.reveal_ttl_seconds == 1_800
    assert config.max_persisted_reveals == 8
    assert config.max_search_calls_per_step == 2
    assert config.max_reveals_per_step == 5
    assert config.max_search_result_chars_per_step == 2_000
    assert config.allow_emergency_eager is False


@pytest.mark.parametrize("mode", ["shadow", "portable", "native_auto"])
def test_tool_exposure_mode_has_an_explicit_environment_override(monkeypatch, mode):
    monkeypatch.setenv("OPENBOX_TOOL_EXPOSURE_MODE", mode)
    monkeypatch.delenv("OPENBOX_ALLOW_EMERGENCY_EAGER", raising=False)

    data = _apply_env_overrides({})

    assert data["tool_exposure"] == {"mode": mode}


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
