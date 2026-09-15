"""Independent capture and administrator rollout controls."""
from dataclasses import dataclass
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SERVER_SPOOL_DIR = Path("/var/lib/openbox/trajectory-spool")
BACKEND_DIR = Path(__file__).resolve().parent.parent
_warned: set[tuple[str, str]] = set()


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


def _selected(name: str, user_id: str | None) -> bool:
    ids = selected_user_ids(name)
    return user_id is None or not ids or user_id in ids


def selected_user_ids(name: str) -> set[str]:
    return {item.strip() for item in os.getenv(name, "").split(",") if item.strip()}


def enabled(user_id: str | None = None) -> bool:
    return _flag("TRAJECTORY_RECORDING_ENABLED") and _selected("TRAJECTORY_RECORD_USER_IDS", user_id)


def admin_enabled(user_id: str | None = None) -> bool:
    return _flag("TRAJECTORY_ADMIN_ENABLED") and _selected("TRAJECTORY_ADMIN_USER_IDS", user_id)


def _invalid(name: str, value: str, fallback) -> None:
    # Settings are read per call; warn once per distinct bad value.
    if (name, value) not in _warned:
        _warned.add((name, value))
        log.warning("Invalid %s=%r; using %r", name, value, fallback)


def integer(name: str, default: int, minimum: int = 1) -> int:
    """An integer setting; a value that is not an integer or is below ``minimum`` falls back to the default."""
    fallback = max(minimum, default)
    raw = os.getenv(name)
    if raw is None:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is None or value < minimum:
        _invalid(name, raw, fallback)
        return fallback
    return value


def pipeline_off() -> bool:
    """``TRAJECTORY_WORKER_MODE=off``: this process runs no emitter, metadata sync or worker."""
    return (os.getenv("TRAJECTORY_WORKER_MODE") or "").strip().lower() == "off"


def spool_dir() -> Path:
    configured = (os.getenv("TRAJECTORY_SPOOL_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    try:
        server = SERVER_SPOOL_DIR.is_dir()
    except OSError:
        # Path.is_dir() raises for EACCES on a parent; that directory is unusable.
        server = False
    if server:
        return SERVER_SPOOL_DIR
    return BACKEND_DIR / ".openbox" / "trajectory-spool"


@dataclass(frozen=True)
class EmitterSettings:
    spool_dir: Path
    queue_bytes: int
    max_event_bytes: int
    file_bytes: int
    file_ms: int
    spool_max_bytes: int
    budget_refresh_ms: int


def emitter_settings() -> EmitterSettings:
    return EmitterSettings(
        spool_dir=spool_dir(),
        queue_bytes=integer("TRAJECTORY_EMIT_QUEUE_BYTES", 64 * 1024 * 1024),
        max_event_bytes=integer("TRAJECTORY_EMIT_MAX_EVENT_BYTES", 32 * 1024 * 1024),
        file_bytes=integer("TRAJECTORY_SPOOL_FILE_BYTES", 8 * 1024 * 1024),
        file_ms=integer("TRAJECTORY_SPOOL_FILE_MS", 1000),
        spool_max_bytes=integer("TRAJECTORY_SPOOL_MAX_BYTES", 2 * 1024 * 1024 * 1024),
        budget_refresh_ms=integer("TRAJECTORY_BUDGET_REFRESH_MS", 5000),
    )
