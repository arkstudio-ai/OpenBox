"""Kernel extension points and the registry an entry point builds.

The contracts are described in ``docs/kernel-refactor/plugin-interfaces.md``.
The kernel defines every interface here and never imports a plugin: an entry
point (``main.py``) builds one ``KernelExtensions``, lets each plugin register
into it, and installs it for the process. Without an installed registry the
accessor returns an empty one whose ports do nothing, so the kernel runs with
no plugin at all.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, ContextManager, Iterator, Mapping, Protocol, runtime_checkable

from core.log import create_logger

log = create_logger("agent.extensions")


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------


class TurnObserver(Protocol):
    """Delivery tier 1: after the caller's commit, in process, at most once.

    A fact recorded with ``db`` is delivered only when that session's outermost
    transaction commits and is dropped on rollback; one without ``db`` is
    delivered now. Implementations never raise.
    """

    async def record(self, type: str, data: dict, *, context: Any = None, db: Any = None,
                     event_id: str | None = None, occurred_at: Any = None) -> None: ...

    def record_stream(self, context: Any, event: dict) -> None: ...

    def emit(self, type: str, data: dict, *, context: Any = None, event_id: str | None = None) -> Any: ...

    def emit_control(self, control: dict, *, db: Any = None) -> Any: ...


class RecordingIdentity(Protocol):
    """The observer's own identity of a write; opaque to the kernel beyond ``derive``/``to_dict``.

    The ``*_in_tx`` methods read or write the business row they are handed in
    the caller's transaction (the recording markers); they never touch the
    observer's own storage.
    """

    def enabled(self, user_id: str | None = None) -> bool: ...
    def current(self) -> Any: ...
    def bind(self, context: Any) -> ContextManager: ...
    def new_identity(self, user_id: str, session_id: str, **fields) -> Any: ...
    def parse_identity(self, saved: Any) -> Any: ...
    def saved_identity(self, saved: Any) -> dict: ...
    def markers(self, saved: Any) -> dict: ...
    async def session_identity(self, db, user_id: str, session_id: str, **ids) -> Any: ...
    async def activity_identity(self, db, user_id: str, session_id: str, *, saved: dict | None = None,
                                **ids) -> Any: ...
    async def mark_recording_in_tx(self, db, execution, *, user_id: str, session_id: str, context) -> None: ...
    async def paused_in_tx(self, db, context) -> bool: ...
    async def baseline_candidate_in_tx(self, db, *, user_id: str, session_id: str, context,
                                       pause: bool = False) -> None: ...
    async def capture_asset_ids_in_tx(self, db, context, asset_ids, *, role: str) -> dict: ...
    def context_of(self, ctx) -> Any: ...
    def attach(self, ctx, context) -> None: ...
    async def context_for_tool(self, ctx) -> Any: ...
    def public_value(self, value: Any) -> Any: ...
    def tool_schema(self, tool_info, name: str) -> dict: ...
    def requested_tool_schema(self, ctx, name: str, tool_info=None) -> tuple[dict, str]: ...
    def timestamp(self, value) -> str | None: ...


class Recorder(TurnObserver, RecordingIdentity, Protocol):
    """What the registry's single recorder slot holds this round."""


class ModelCall(Protocol):
    """Observer of one provider request, opened before it is sent."""

    context: Any
    finished: bool

    def stream_chunks(self, stream, block_builder: Callable[[Any], list]) -> AsyncIterator: ...
    async def chunk(self, raw: Any, *, blocks: list | None = None) -> None: ...
    async def capture_usage(self, usage: dict) -> None: ...
    async def finish(self, status: str, *, reason: str | None = None, error: BaseException | None = None) -> None: ...
    async def route_changed(self, reason: str) -> None: ...


class ToolBody(Protocol):
    """Observer of one tool call's output."""

    closed: bool
    finished: bool

    async def update(self, output: str) -> None: ...
    async def finish(self, output: Any, **fields: Any) -> None: ...
    async def close(self) -> None: ...


class CallWrapper(Protocol):
    """Sees every model call and tool body, their results and usage."""

    async def begin_model_call(self, ctx, *, purpose: str, model_id: str, payload: Mapping,
                               capture_level: str) -> ModelCall: ...
    def chunk_blocks(self, kind: str) -> Callable[[Any], list]: ...
    async def capture_billing(self, context, meter, usage, credits, purpose: str) -> None: ...
    def bind_model_billing(self, ctx, meter) -> None: ...
    def active_request(self, ctx) -> Any: ...
    def note_model_attempt(self, ctx, attempt: int) -> None: ...
    def media_capture(self, ctx, enabled: bool) -> tuple[dict | None, dict | None]: ...
    def note_tool_request(self, ctx, tool_info, provider_call_id, arguments_raw) -> None: ...
    def pending_tool_request(self, ctx) -> tuple: ...
    def begin_tool_body(self, ctx, context, *, tool: str, owner: Any, stopped=None) -> ToolBody: ...
    def tool_body(self, ctx, context, *, tool: str, owner: Any) -> ToolBody: ...
    def running_tool_body(self, ctx) -> ToolBody | None: ...
    def retain_full_output(self, ctx, output: str | None) -> None: ...
    def retained_output(self, ctx, fallback: str) -> str: ...


class TimingPort(Protocol):
    """Opens a run timer (``core.timing.RunTimer``) per run and per speculative dependency."""

    def start_run(self) -> Any: ...
    def finish_run(self, timer, *, session_id: str, run_id: str | None, ended: float) -> None: ...
    def start_dependency(self, parent: Any) -> Any: ...


@runtime_checkable
class RecoveryHook(Protocol):
    """Runs after the kernel's fixed recovery order, in registration order."""

    name: str

    async def recover(self) -> int: ...


#: Where the kernel's project brief sits among plugin prompt sections. A section
#: whose ``order`` is lower renders after the kernel's static parts and the
#: account identity, before the brief; the others render after it, at the end.
PROMPT_ORDER_PROJECT_BRIEF = 500


@dataclass(frozen=True, slots=True)
class PromptRequest:
    """What a prompt section sees of one system prompt assembly (``agent.loop._build_system_prompt``)."""

    agent_name: str
    model_id: str
    user_id: str = ""
    project_id: str = ""
    workspace_id: str = ""
    session_kind: str | None = None
    # Still decided by the agent loop this round (docs/KERNEL_DSH_COMPARISON_20261128.md 9.1).
    memory_isolated: bool = False
    include_user_memory: bool = True


class PromptSection(Protocol):
    """A system prompt section rendered per assembly; None or "" adds nothing."""

    name: str
    order: int

    async def render(self, request: PromptRequest) -> str | None: ...


class ToolPack(Protocol):
    """A group of tools: registered with the built-ins, narrowed per step, checked before each call."""

    name: str
    capability: str | None

    def tools(self) -> Mapping[str, Any]: ...

    def admit(self, tools: dict[str, Any], *, user_id: str | None) -> dict[str, Any]:
        """The step's resolved tools without the ones this pack does not offer now."""

    async def before_call(self, tool_id: str, args: dict, ctx) -> Any:
        """A refusal (``ToolResult``) of this call, or None to go on to the permission policy."""


class Plugin(Protocol):
    """A first-party module registered once at startup.

    Optional hooks, each run at a fixed position of the entry point's lifespan:
    ``mount(app)`` adds routes; ``startup(app)`` runs once the database engine
    is ready; ``start_background(app)`` runs after the kernel's own background
    services started; ``stop_background(app)`` returns named stop operations
    that the entry point runs together with its own; ``shutdown(app)`` runs
    last, after agent loops drained.
    """

    name: str

    def register(self, registry: "KernelExtensions") -> None: ...


# ---------------------------------------------------------------------------
# Empty implementations: the kernel with no plugin
# ---------------------------------------------------------------------------


class _NullRecorder:
    async def record(self, type, data, *, context=None, db=None, event_id=None, occurred_at=None):
        return None

    def record_stream(self, context, event):
        return None

    def emit(self, type, data, *, context=None, event_id=None):
        return None

    def emit_control(self, control, *, db=None):
        return False

    def enabled(self, user_id=None):
        return False

    def current(self):
        return None

    def bind(self, context):
        return nullcontext(context)

    def new_identity(self, user_id, session_id, **fields):
        return None

    def parse_identity(self, saved):
        return None

    def saved_identity(self, saved):
        return {}

    def markers(self, saved):
        return {}

    async def session_identity(self, db, user_id, session_id, **ids):
        return None

    async def activity_identity(self, db, user_id, session_id, *, saved=None, **ids):
        return None

    async def mark_recording_in_tx(self, db, execution, *, user_id, session_id, context):
        return None

    async def paused_in_tx(self, db, context):
        return False

    async def baseline_candidate_in_tx(self, db, *, user_id, session_id, context, pause=False):
        return None

    async def capture_asset_ids_in_tx(self, db, context, asset_ids, *, role):
        return {}

    def context_of(self, ctx):
        return None

    def attach(self, ctx, context):
        return None

    async def context_for_tool(self, ctx):
        return None

    def public_value(self, value):
        return value

    def tool_schema(self, tool_info, name):
        return {}

    def requested_tool_schema(self, ctx, name, tool_info=None):
        return {}, "executor_registry"

    def timestamp(self, value):
        return None


class _NullModelCall:
    context = None

    def __init__(self):
        self.finished = False

    async def stream_chunks(self, stream, block_builder):
        from agent.timing import provider_chunks
        async for chunk in provider_chunks(stream):
            yield chunk

    async def chunk(self, raw, *, blocks=None):
        return None

    async def capture_usage(self, usage):
        return None

    async def finish(self, status, *, reason=None, error=None):
        self.finished = True

    async def route_changed(self, reason):
        return None


class _NullToolBody:
    def __init__(self):
        self.closed = False
        self.finished = False

    async def update(self, output):
        return None

    async def append(self, chunk, *, output=None):
        return None

    async def finish(self, output, **fields):
        self.finished = self.closed = True

    async def close(self):
        self.closed = True


def _no_blocks(_chunk) -> list:
    return []


class _NullCallWrapper:
    async def begin_model_call(self, ctx, *, purpose, model_id, payload, capture_level):
        return _NullModelCall()

    def chunk_blocks(self, kind):
        return _no_blocks

    async def capture_billing(self, context, meter, usage, credits, purpose):
        return None

    def bind_model_billing(self, ctx, meter):
        return None

    def active_request(self, ctx):
        return None

    def note_model_attempt(self, ctx, attempt):
        return None

    def media_capture(self, ctx, enabled):
        return None, None

    def note_tool_request(self, ctx, tool_info, provider_call_id, arguments_raw):
        return None

    def pending_tool_request(self, ctx):
        return None, None, None

    def begin_tool_body(self, ctx, context, *, tool, owner, stopped=None):
        return _NullToolBody()

    def tool_body(self, ctx, context, *, tool, owner):
        return _NullToolBody()

    def running_tool_body(self, ctx):
        return None

    def retain_full_output(self, ctx, output):
        return None

    def retained_output(self, ctx, fallback):
        return fallback


NULL_RECORDER = _NullRecorder()
NULL_CALL_WRAPPER = _NullCallWrapper()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class KernelExtensions:
    """Everything registered for one process; built at startup, sealed once installed."""

    recorder: Recorder = field(default=NULL_RECORDER)
    call_wrapper: CallWrapper = field(default=NULL_CALL_WRAPPER)
    timing: TimingPort | None = None
    prompt_sections: tuple[PromptSection, ...] = ()
    tool_packs: tuple[ToolPack, ...] = ()
    recovery_hooks: tuple[RecoveryHook, ...] = ()
    plugins: tuple[Plugin, ...] = ()
    _sealed: bool = field(default=False, repr=False)

    def _open(self) -> None:
        if self._sealed:
            raise RuntimeError("kernel extensions are registered only at startup")

    def add(self, plugin: Plugin) -> "KernelExtensions":
        self._open()
        if any(existing.name == plugin.name for existing in self.plugins):
            raise ValueError(f"plugin {plugin.name!r} is already registered")
        plugin.register(self)
        self.plugins = (*self.plugins, plugin)
        return self

    def set_recorder(self, recorder: Recorder) -> None:
        self._open()
        if self.recorder is not NULL_RECORDER:
            raise ValueError("one turn observer is supported this round")
        self.recorder = recorder

    def set_call_wrapper(self, wrapper: CallWrapper) -> None:
        self._open()
        if self.call_wrapper is not NULL_CALL_WRAPPER:
            raise ValueError("one call wrapper is supported this round")
        self.call_wrapper = wrapper

    def set_timing(self, timing: TimingPort) -> None:
        self._open()
        if self.timing is not None:
            raise ValueError("one timing port is supported")
        self.timing = timing

    def add_recovery_hook(self, hook: RecoveryHook) -> None:
        self._open()
        self.recovery_hooks = (*self.recovery_hooks, hook)

    def add_prompt_section(self, section: PromptSection) -> None:
        self._open()
        self.prompt_sections = (*self.prompt_sections, section)

    def add_tool_pack(self, pack: ToolPack) -> None:
        self._open()
        if any(existing.name == pack.name for existing in self.tool_packs):
            raise ValueError(f"tool pack {pack.name!r} is already registered")
        self.tool_packs = (*self.tool_packs, pack)

    def seal(self) -> None:
        self._sealed = True

    def mount(self, app) -> None:
        """Let plugins add their routes, at the position the entry point calls this."""
        for plugin in self.plugins:
            mount = getattr(plugin, "mount", None)
            if mount is not None:
                mount(app)

    async def startup(self, app) -> None:
        for plugin in self.plugins:
            start = getattr(plugin, "startup", None)
            if start is not None:
                await start(app)

    async def start_background(self, app) -> None:
        """Plugins' background workers, after the kernel's own background services started."""
        for plugin in self.plugins:
            start = getattr(plugin, "start_background", None)
            if start is not None:
                await start(app)

    def stop_background(self, app) -> dict[str, Any]:
        """Plugins' named stop operations, for the entry point to run alongside its own."""
        stops: dict[str, Any] = {}
        for plugin in self.plugins:
            stop = getattr(plugin, "stop_background", None)
            if stop is None:
                continue
            for name, operation in stop(app).items():
                if name in stops:
                    raise ValueError(f"background service {name!r} is registered twice")
                stops[name] = operation
        return stops

    async def shutdown(self, app) -> None:
        for plugin in reversed(self.plugins):
            stop = getattr(plugin, "shutdown", None)
            if stop is not None:
                await stop(app)


_EMPTY = KernelExtensions(_sealed=True)
#: The registry the entry point installed; one slot, nothing appends to it.
_installed: KernelExtensions = _EMPTY


def extensions() -> KernelExtensions:
    return _installed


def install(registry: KernelExtensions | None) -> KernelExtensions:
    """Install ``registry`` for the process (None installs the empty one); returns the previous one."""
    global _installed
    previous = _installed
    registry = registry if registry is not None else _EMPTY
    registry.seal()
    _installed = registry
    return previous


@contextmanager
def use(registry: KernelExtensions | None) -> Iterator[KernelExtensions]:
    """Run a block with ``registry`` installed (tests and benchmarks)."""
    previous = install(registry)
    try:
        yield extensions()
    finally:
        install(previous)


async def run_recovery_hooks() -> int:
    """Plugins' recovery, after the kernel's own fixed order; one failure never stops the rest."""
    changed = 0
    for hook in extensions().recovery_hooks:
        try:
            changed += int(await hook.recover() or 0)
        except Exception:
            log.exception("Plugin recovery deferred hook=%s", getattr(hook, "name", type(hook).__name__))
    return changed


async def render_prompt_sections(request: PromptRequest, *, start: int | None = None,
                                 stop: int | None = None) -> list[str]:
    """Registered sections with ``start <= order < stop``, by order then registration.

    A section that fails is skipped and logged; the prompt is still built.
    """
    chosen = [(section.order, index, section) for index, section in enumerate(extensions().prompt_sections)
              if (start is None or section.order >= start) and (stop is None or section.order < stop)]
    parts = []
    for _order, _index, section in sorted(chosen, key=lambda item: item[:2]):
        try:
            text = await section.render(request)
        except Exception as exc:
            log.warning("Prompt section skipped section=%s error_type=%s",
                        getattr(section, "name", type(section).__name__), type(exc).__name__)
            continue
        if text:
            parts.append(text)
    return parts


def pack_tools() -> list:
    """The installed packs' tools, in registration order; a pack that cannot list them registers none."""
    found = []
    for pack in extensions().tool_packs:
        try:
            found.extend(pack.tools().values())
        except Exception:
            log.exception("Tool pack not registered pack=%s", pack.name)
    return found


def admit_pack_tools(tools: dict, *, user_id: str | None) -> dict:
    """A step's resolved tools after every pack withheld what it does not offer now.

    A pack that fails withholds its own tools (fail closed).
    """
    for pack in extensions().tool_packs:
        try:
            tools = pack.admit(tools, user_id=user_id)
        except Exception:
            log.exception("Tool pack withheld after a failure pack=%s", pack.name)
            try:
                own = set(pack.tools())
            except Exception:
                own = set()
            tools = {tool_id: tool for tool_id, tool in tools.items() if tool_id not in own}
    return tools


async def check_before_call(tool_id: str, args: dict, ctx) -> Any:
    """The first pack refusal of this call, or None. A pack's exception reaches the caller, never allows."""
    for pack in extensions().tool_packs:
        refusal = await pack.before_call(tool_id, args, ctx)
        if refusal is not None:
            return refusal
    return None


# ---------------------------------------------------------------------------
# Plugin-private state on a ToolContext
# ---------------------------------------------------------------------------
#
# ``ToolContext._plugin_state`` maps a plugin's keys to its values; the kernel
# never reads it. A write replaces the whole mapping instead of mutating it, so
# ``copy.copy(ctx)`` copies keep the semantics of separate attributes: a write
# on a copy never changes the original.


def plugin_state(ctx, key: str, default: Any = None) -> Any:
    state = getattr(ctx, "_plugin_state", None)
    if not state:
        return default
    return state.get(key, default)


def set_plugin_state(ctx, key: str, value: Any) -> None:
    state = getattr(ctx, "_plugin_state", None) or {}
    ctx._plugin_state = {**state, key: value}
