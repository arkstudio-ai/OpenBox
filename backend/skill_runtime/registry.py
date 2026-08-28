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


@dataclass(frozen=True)
class BuiltinHandler:
    skill_key: str
    handler: HandlerFn
    handler_version: int = 1


_handlers: dict[str, BuiltinHandler] = {}

#: Builtin skill packages shipped in the image. Extended in place by later
#: phases (video-production lands here); never fed from user data.
_BUILTIN_MODULES: tuple[str, ...] = ()

_loaded = False


def register_builtin(skill_key: str, handler: HandlerFn, *, handler_version: int = 1) -> None:
    if skill_key in _handlers:
        raise ValueError(f"builtin handler already registered: {skill_key}")
    _handlers[skill_key] = BuiltinHandler(skill_key, handler, handler_version)
    log.debug(f"Registered builtin handler: {skill_key} v{handler_version}")


def resolve(skill_key: str) -> BuiltinHandler | None:
    return _handlers.get(skill_key)


def load_builtin_handlers() -> int:
    """Import every builtin skill package exactly once; their module bodies
    call register_builtin. Returns how many handlers are registered."""
    global _loaded
    if not _loaded:
        import importlib

        for module_name in _BUILTIN_MODULES:
            importlib.import_module(module_name)
        _loaded = True
    return len(_handlers)
