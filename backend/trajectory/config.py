"""Independent capture and administrator rollout controls."""
import os


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


def integer(name: str, default: int, minimum: int = 1) -> int:
    return max(minimum, int(os.getenv(name, str(default))))
