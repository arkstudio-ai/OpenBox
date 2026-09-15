"""Worker settings (SPEC §13): defaults, minimums, invalid values and the cached accessor."""
from pathlib import Path

import pytest

from trajectory.worker import settings as settings_module
from trajectory.worker.settings import WorkerSettings, get_worker_settings, reset_worker_settings

_VARS = ("JWT_SECRET", "TRAJECTORY_WORKER_MODE", "TRAJECTORY_DATABASE_URL", "OSS_BUCKET", "OSS_REGION",
         "TRAJECTORY_OSS_REGION", "TRAJECTORY_OSS_ENDPOINT", "TRAJECTORY_SPOOL_DIR", "REDIS_URL",
         "TRAJECTORY_INGEST_BATCH_LINES", "TRAJECTORY_SPOOL_ABANDON_SECONDS", "TRAJECTORY_BLOB_PROVIDER",
         "TRAJECTORY_OSS_INTERNAL", "INTERNAL_API_TOKEN", "TRAJECTORY_SPOOL_QUARANTINE_MAX_BYTES",
         "TRAJECTORY_SPOOL_QUARANTINE_RETENTION_DAYS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    reset_worker_settings()
    yield
    reset_worker_settings()


def test_defaults_follow_the_configuration_reference(tmp_path):
    settings = WorkerSettings.from_env()
    assert settings.mode == "embedded"
    assert settings.database_url == settings_module.DEFAULT_EMBEDDED_DATABASE_URL
    assert settings.spool_dir == tmp_path / "spool"
    assert (settings.db_pool_size, settings.db_pool_overflow) == (5, 5)
    assert (settings.host, settings.port) == ("0.0.0.0", 8090)
    assert settings.ingest_poll_ms == 200
    assert (settings.ingest_batch_lines, settings.ingest_batch_bytes) == (2000, 16777216)
    assert settings.spool_abandon_seconds == 60
    assert settings.ingest_max_batch_failures == 10
    assert (settings.quarantine_max_bytes, settings.quarantine_retention_days) == (268435456, 7)
    assert (settings.inline_bytes, settings.record_inline_bytes) == (65536, 16384)
    assert (settings.projection_batch_ms, settings.projection_batch_events) == (250, 200)
    assert settings.checkpoint_interval == 1000
    assert (settings.segment_events, settings.segment_max_bytes, settings.segment_idle_seconds) == (1000, 4194304, 300)
    # The segment and blob cache sizes are read where the caches live (repository.py, payload.py).
    assert not hasattr(settings, "segment_cache_bytes") and not hasattr(settings, "blob_cache_bytes")
    assert (settings.hot_days, settings.dedupe_days) == (7, 30)
    assert (settings.content_retention_days, settings.export_retention_days) == (180, 30)
    assert settings.budget_trajectory_events == 50000
    assert settings.budget_trajectory_bytes == 209715200
    assert settings.budget_trajectory_block_bytes == 1073741824
    assert settings.budget_user_daily_bytes == 2147483648
    assert settings.blob_provider == "local"
    assert settings.blob_local_path == Path(settings_module.DEFAULT_BLOB_LOCAL_PATH)
    assert (settings.oss_prefix, settings.oss_internal, settings.oss_endpoint) == ("trajectories/", True, "")
    assert settings.backend_internal_url == "http://backend:8080"
    assert settings.auth_cache_seconds == 5
    assert settings.blob_fault is None
    assert (settings.cms_region, settings.cms_group_id) == ("cn-shanghai", None)
    assert settings.embedded is True


def test_server_mode_requires_an_explicit_database_and_hides_secrets(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "jwt-secret-value")
    monkeypatch.setenv("REDIS_URL", "redis://:password@redis:6379/0")
    monkeypatch.setenv("OSS_REGION", "cn-shanghai")
    settings = WorkerSettings.from_env()
    assert settings.mode == "external"
    assert settings.database_url is None
    assert settings.oss_region == "cn-shanghai"
    assert settings.oss_endpoint == "oss-cn-shanghai.aliyuncs.com"
    text = repr(settings)
    assert "jwt-secret-value" not in text and "password" not in text


def test_invalid_values_fall_back_to_their_defaults(monkeypatch, caplog):
    monkeypatch.setenv("TRAJECTORY_WORKER_MODE", "sideways")
    monkeypatch.setenv("TRAJECTORY_INGEST_BATCH_LINES", "lots")
    monkeypatch.setenv("TRAJECTORY_SPOOL_ABANDON_SECONDS", "0")
    monkeypatch.setenv("TRAJECTORY_BLOB_PROVIDER", "ftp")
    monkeypatch.setenv("TRAJECTORY_OSS_INTERNAL", "maybe")
    monkeypatch.setenv("TRAJECTORY_SPOOL_QUARANTINE_MAX_BYTES", "1048576")
    monkeypatch.setenv("TRAJECTORY_SPOOL_QUARANTINE_RETENTION_DAYS", "0")
    settings = WorkerSettings.from_env()
    assert (settings.quarantine_max_bytes, settings.quarantine_retention_days) == (1048576, 7)
    assert settings.mode == "embedded"
    assert settings.ingest_batch_lines == 2000
    assert settings.spool_abandon_seconds == 60
    assert settings.blob_provider == "local"
    assert settings.oss_internal is True


def test_accessor_caches_until_reset(monkeypatch):
    first = get_worker_settings()
    monkeypatch.setenv("TRAJECTORY_WORKER_MODE", "off")
    assert get_worker_settings() is first
    reset_worker_settings()
    assert get_worker_settings().mode == "off"
