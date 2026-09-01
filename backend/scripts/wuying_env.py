"""One deterministic environment-file contract for WUYING helper scripts."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


BACKEND_DIR = Path(__file__).resolve().parent.parent


def environment_file() -> Path | None:
    """Resolve an explicit profile, then the supported local defaults."""
    configured = os.environ.get("OPENBOX_ENV_FILE", "").strip()
    candidates = [Path(configured)] if configured else [
        BACKEND_DIR / ".env",
        BACKEND_DIR / ".env.wuying-dev",
    ]
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else BACKEND_DIR / candidate
        if path.is_file():
            return path
    return None


def base_environment_file() -> Path | None:
    """Return an explicitly requested base profile, never an implicit one."""
    configured = os.environ.get("OPENBOX_BASE_ENV_FILE", "").strip()
    if not configured:
        return None
    candidate = Path(configured)
    path = candidate if candidate.is_absolute() else BACKEND_DIR / candidate
    return path if path.is_file() else None


def _values(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    return {
        key: value
        for key, raw in dotenv_values(path).items()
        if key and isinstance(raw, str) and (value := raw.strip())
    }


def load_environment() -> Path | None:
    """Load ``process > selected profile > explicit base profile``.

    ``OPENBOX_BASE_ENV_FILE`` is intentionally opt-in.  The dev launcher uses
    it to reuse model/database credentials from ``.env`` while the selected
    WUYING dev profile replaces every execution-plane value.  An explicit
    process value such as ``JWT_SECRET=`` remains authoritative.
    """
    selected = environment_file()
    explicit = set(os.environ)
    base_values = _values(base_environment_file())
    selected_values = _values(selected)
    for key, value in base_values.items():
        if key not in explicit:
            os.environ.setdefault(key, value)
    for key, value in selected_values.items():
        if key not in explicit:
            os.environ[key] = value
    return selected


def environment_value(key: str) -> str:
    """Read one value using the same deterministic profile precedence."""
    explicit = os.environ.get(key)
    if explicit is not None:
        return explicit.strip()
    selected = _values(environment_file()).get(key)
    if selected is not None:
        return selected
    return _values(base_environment_file()).get(key, "")
