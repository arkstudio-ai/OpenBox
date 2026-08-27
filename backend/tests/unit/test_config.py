"""Test new config fields are present and have correct defaults."""
from core.config import OpenBoxConfig


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


def test_video_transcription_config_defaults():
    config = OpenBoxConfig()
    assert config.video_transcription.engine == "dashscope"
    assert config.video_transcription.base_url == "https://dashscope.aliyuncs.com"
    assert config.video_transcription.model == "fun-asr"
    assert config.video_transcription.timeout_seconds == 180
    assert config.video_transcription.poll_interval_seconds == 1.0
    assert config.video_transcription.similarity_threshold == 0.90
