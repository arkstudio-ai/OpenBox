"""Worker settings (SPEC §13), read once from the environment.

Integer settings have a minimum of 1; invalid values fall back to the default
with a warning (``trajectory.config.integer``). The recording and admin
switches (``TRAJECTORY_RECORDING_ENABLED`` and friends) are not cached here:
they are shared with the backend and evaluated per call by
``trajectory.config``.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from trajectory.config import BACKEND_DIR, integer, spool_dir

log = logging.getLogger(__name__)

WORKER_MODES = ("external", "embedded", "off")
BLOB_PROVIDERS = ("local", "oss")
DEFAULT_EMBEDDED_DATABASE_URL = f"sqlite+aiosqlite:///{BACKEND_DIR / '.openbox' / 'trajectory.db'}"
DEFAULT_BLOB_LOCAL_PATH = BACKEND_DIR / ".openbox" / "trajectory-blobs"
_warned: set[tuple[str, str]] = set()


def _text(name: str, default: str | None = None) -> str | None:
    value = (os.getenv(name) or "").strip()
    return value or default


def _choice(name: str, choices: tuple[str, ...], default: str) -> str:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in choices:
        return value
    if (name, raw) not in _warned:
        _warned.add((name, raw))
        log.warning("Invalid %s=%r; using %r", name, raw, default)
    return default


def _flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    if (name, raw) not in _warned:
        _warned.add((name, raw))
        log.warning("Invalid %s=%r; using %r", name, raw, default)
    return default


@dataclass(frozen=True)
class WorkerSettings:
    """Every worker setting of SPEC §13 with its default."""

    mode: str
    spool_dir: Path
    #: None in external mode without TRAJECTORY_DATABASE_URL (the process must refuse to start).
    database_url: str | None = field(repr=False)
    db_pool_size: int
    db_pool_overflow: int
    host: str
    port: int
    ingest_poll_ms: int
    ingest_batch_lines: int
    ingest_batch_bytes: int
    spool_abandon_seconds: int
    inline_bytes: int
    record_inline_bytes: int
    projection_batch_ms: int
    projection_batch_events: int
    checkpoint_interval: int
    segment_events: int
    segment_max_bytes: int
    segment_idle_seconds: int
    segment_cache_bytes: int
    blob_cache_bytes: int
    hot_days: int
    dedupe_days: int
    content_retention_days: int
    export_retention_days: int
    budget_trajectory_events: int
    budget_trajectory_bytes: int
    budget_trajectory_block_bytes: int
    budget_user_daily_bytes: int
    blob_provider: str
    blob_local_path: Path
    oss_bucket: str
    oss_region: str
    oss_endpoint: str
    oss_prefix: str
    oss_internal: bool
    backend_internal_url: str
    auth_cache_seconds: int
    blob_fault: str | None
    cms_region: str
    cms_group_id: str | None
    redis_url: str | None = field(default=None, repr=False)
    jwt_secret: str | None = field(default=None, repr=False)
    internal_api_token: str | None = field(default=None, repr=False)
    #: TRAJECTORY_EXPORT_MAX_BYTES: size cap of one export archive (defaulted, so it follows the secrets).
    export_max_bytes: int = 256 * 1024 * 1024

    @classmethod
    def from_env(cls) -> WorkerSettings:
        jwt_secret = _text("JWT_SECRET")
        mode = _choice("TRAJECTORY_WORKER_MODE", WORKER_MODES, "external" if jwt_secret else "embedded")
        database_url = _text("TRAJECTORY_DATABASE_URL")
        if database_url is None and mode != "external":
            database_url = DEFAULT_EMBEDDED_DATABASE_URL
        region = _text("TRAJECTORY_OSS_REGION") or _text("OSS_REGION") or ""
        return cls(
            mode=mode,
            spool_dir=spool_dir(),
            database_url=database_url,
            db_pool_size=integer("TRAJECTORY_DB_POOL_SIZE", 5),
            db_pool_overflow=integer("TRAJECTORY_DB_POOL_OVERFLOW", 5),
            host=_text("TRAJECTORY_WORKER_HOST", "0.0.0.0"),
            port=integer("TRAJECTORY_WORKER_PORT", 8090),
            ingest_poll_ms=integer("TRAJECTORY_INGEST_POLL_MS", 200),
            ingest_batch_lines=integer("TRAJECTORY_INGEST_BATCH_LINES", 2000),
            ingest_batch_bytes=integer("TRAJECTORY_INGEST_BATCH_BYTES", 16 * 1024 * 1024),
            spool_abandon_seconds=integer("TRAJECTORY_SPOOL_ABANDON_SECONDS", 60),
            inline_bytes=integer("TRAJECTORY_INLINE_BYTES", 65536),
            record_inline_bytes=integer("TRAJECTORY_RECORD_INLINE_BYTES", 16384),
            projection_batch_ms=integer("TRAJECTORY_PROJECTION_BATCH_MS", 250),
            projection_batch_events=integer("TRAJECTORY_PROJECTION_BATCH_EVENTS", 200),
            checkpoint_interval=integer("TRAJECTORY_CHECKPOINT_INTERVAL", 1000),
            segment_events=integer("TRAJECTORY_SEGMENT_EVENTS", 1000),
            segment_max_bytes=integer("TRAJECTORY_SEGMENT_MAX_BYTES", 4 * 1024 * 1024),
            segment_idle_seconds=integer("TRAJECTORY_SEGMENT_IDLE_SECONDS", 300),
            segment_cache_bytes=integer("TRAJECTORY_SEGMENT_CACHE_BYTES", 128 * 1024 * 1024),
            blob_cache_bytes=integer("TRAJECTORY_BLOB_CACHE_BYTES", 256 * 1024 * 1024),
            hot_days=integer("TRAJECTORY_HOT_DAYS", 7),
            dedupe_days=integer("TRAJECTORY_DEDUPE_DAYS", 30),
            content_retention_days=integer("TRAJECTORY_CONTENT_RETENTION_DAYS", 180),
            export_retention_days=integer("TRAJECTORY_EXPORT_RETENTION_DAYS", 30),
            export_max_bytes=integer("TRAJECTORY_EXPORT_MAX_BYTES", 256 * 1024 * 1024),
            budget_trajectory_events=integer("TRAJECTORY_BUDGET_TRAJECTORY_EVENTS", 50000),
            budget_trajectory_bytes=integer("TRAJECTORY_BUDGET_TRAJECTORY_BYTES", 200 * 1024 * 1024),
            budget_trajectory_block_bytes=integer("TRAJECTORY_BUDGET_TRAJECTORY_BLOCK_BYTES", 1024 * 1024 * 1024),
            budget_user_daily_bytes=integer("TRAJECTORY_BUDGET_USER_DAILY_BYTES", 2 * 1024 * 1024 * 1024),
            blob_provider=_choice("TRAJECTORY_BLOB_PROVIDER", BLOB_PROVIDERS, "local"),
            blob_local_path=Path(_text("TRAJECTORY_BLOB_LOCAL_PATH") or DEFAULT_BLOB_LOCAL_PATH).expanduser(),
            oss_bucket=_text("TRAJECTORY_OSS_BUCKET") or _text("OSS_BUCKET") or "",
            oss_region=region,
            oss_endpoint=_text("TRAJECTORY_OSS_ENDPOINT") or (f"oss-{region}.aliyuncs.com" if region else ""),
            oss_prefix=_text("TRAJECTORY_OSS_PREFIX", "trajectories/"),
            oss_internal=_flag("TRAJECTORY_OSS_INTERNAL", True),
            backend_internal_url=_text("TRAJECTORY_BACKEND_INTERNAL_URL", "http://backend:8080").rstrip("/"),
            auth_cache_seconds=integer("TRAJECTORY_AUTH_CACHE_SECONDS", 5),
            blob_fault=_text("TRAJECTORY_BLOB_FAULT"),
            cms_region=_text("TRAJECTORY_CMS_REGION", "cn-shanghai"),
            cms_group_id=_text("TRAJECTORY_CMS_GROUP_ID"),
            redis_url=_text("REDIS_URL"),
            jwt_secret=jwt_secret,
            internal_api_token=_text("INTERNAL_API_TOKEN"),
        )

    @property
    def embedded(self) -> bool:
        return self.mode == "embedded"


_settings: WorkerSettings | None = None
_settings_lock = threading.Lock()


def get_worker_settings() -> WorkerSettings:
    """Settings parsed on first use and cached for the process."""
    global _settings
    settings = _settings
    if settings is None:
        with _settings_lock:
            if _settings is None:
                _settings = WorkerSettings.from_env()
            settings = _settings
    return settings


def reset_worker_settings() -> None:
    """Forget the cached settings (tests; the next call re-reads the environment)."""
    global _settings
    with _settings_lock:
        _settings = None
