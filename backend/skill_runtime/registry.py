"""Static allowlist of builtin skill handlers.

Only code compiled into the image may register here, and only at import time
from the fixed module list below — a database string can never become a Python
import path (§4.4). Sandbox skills go through the sandbox runtime instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.log import create_logger

log = create_logger("skill_runtime.registry")

#: (ctx, operation, payload, checkpoint) -> Outcome
HandlerFn = Callable[[Any, str, dict, dict], Awaitable[Any]]
StartupValidator = Callable[[Any], None]


@dataclass(frozen=True)
class BuiltinHandler:
    skill_key: str
    handler: HandlerFn
    handler_version: int = 1


_handlers: dict[tuple[str, int], BuiltinHandler] = {}
_startup_validators: dict[str, StartupValidator] = {}

#: Builtin skill packages shipped in the image. Extended in place by later
#: phases (video-production lands here); never fed from user data.
_BUILTIN_MODULES: tuple[str, ...] = (
    "builtin_skills.demo_echo.handlers",
    "builtin_skills.video_production.handlers",
)

_loaded = False


def register_builtin(
    skill_key: str,
    handler: HandlerFn,
    *,
    handler_version: int = 1,
    compatible_versions: tuple[int, ...] = (),
) -> None:
    """Register the exact handler versions this image can safely execute.

    Rolling deploys routinely leave old-version jobs in the ledger. Explicit
    aliases let an implementation declare N-1 compatibility; silently routing
    every version to the newest function would deserialize an incompatible
    checkpoint with no evidence that it is safe.
    """
    versions = tuple(dict.fromkeys((handler_version, *compatible_versions)))
    for version in versions:
        key = (skill_key, version)
        if key in _handlers:
            raise ValueError(f"builtin handler already registered: {skill_key} v{version}")
        _handlers[key] = BuiltinHandler(skill_key, handler, version)
    log.debug(f"Registered builtin handler: {skill_key} versions={versions}")


def resolve(skill_key: str, handler_version: int) -> BuiltinHandler | None:
    return _handlers.get((skill_key, handler_version))


def register_startup_validator(skill_key: str, validator: StartupValidator) -> None:
    """Let a builtin package validate its own optional dependencies.

    The worker runtime calls this registry generically; it must never import a
    domain package (video, documents, etc.) or know which secrets it needs.
    """
    if skill_key in _startup_validators:
        raise ValueError(f"startup validator already registered: {skill_key}")
    _startup_validators[skill_key] = validator


def validate_runtime_dependencies(config) -> None:
    for skill_key, validator in sorted(_startup_validators.items()):
        try:
            validator(config)
        except Exception as exc:
            raise RuntimeError(f"builtin dependency check failed for {skill_key}: {exc}") from exc


def load_builtin_handlers() -> int:
    """Import every builtin skill package exactly once; their module bodies
    call register_builtin. Returns how many handlers are registered."""
    global _loaded
    if not _loaded:
        import importlib

        for module_name in _BUILTIN_MODULES:
            importlib.import_module(module_name)
        from skill_runtime.manifest import load_builtin_manifests

        missing = [
            f"{manifest.skill_key} v{manifest.runtime.handlerVersion}"
            for manifest in load_builtin_manifests().values()
            if manifest.runtime.kind == "internal"
            and resolve(manifest.skill_key, manifest.runtime.handlerVersion) is None
        ]
        if missing:
            raise RuntimeError(
                "builtin manifest/handler version mismatch: " + ", ".join(sorted(missing))
            )
        _loaded = True
    return len({handler.skill_key for handler in _handlers.values()})
