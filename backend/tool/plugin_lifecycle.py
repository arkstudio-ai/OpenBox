"""Generation-scoped lifecycle for trusted in-process platform plugins.

The lifecycle deliberately solves resource ownership, hot replacement, and
in-flight safety.  It does *not* sandbox plugin Python: manifests remain an
administrator-controlled trust boundary and imported code has backend process
authority.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import threading
from collections.abc import AsyncIterable, Callable, Iterable
from dataclasses import dataclass, field
from types import ModuleType
from types import MappingProxyType
from typing import Any

from tool.tool import ToolInfo


log = logging.getLogger("tool.plugin_lifecycle")

MAX_DISPOSERS_PER_GENERATION = 256
QUIESCENCE_DIAGNOSTIC_SECONDS = 30.0

Disposer = Callable[[], Any]


class PluginGenerationRetired(RuntimeError):
    """A resolved tool belongs to a generation that no longer accepts work."""


@dataclass(frozen=True)
class PluginDependency:
    """Read-only dependency identity exposed during plugin setup."""

    name: str
    version: str
    generation_id: str
    tool_ids: tuple[str, ...]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class PluginGeneration:
    """One immutable code/config generation and all resources it owns.

    A generation is staged with ``accepting=False``.  Registry commit activates
    it and retires the previous generation in the same pointer-swap critical
    section.  Tool wrappers pin this object for the complete invocation, so
    teardown cannot clear modules or resources beneath running calls.
    """

    name: str
    version: str
    entrypoints: tuple[str, ...]
    legacy: bool
    dependencies: tuple[str, ...]
    dependency_generation_ids: tuple[tuple[str, str], ...]
    manifest_digest: str
    source_digest: str
    generation_id: str
    tools: tuple[tuple[str, ToolInfo], ...] = ()
    module_ids: tuple[str, ...] = ()
    module_refs: tuple[ModuleType, ...] = ()
    _dispose_stack: list[Disposer] = field(default_factory=list, repr=False)
    _lock: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.RLock()),
        init=False,
        repr=False,
    )
    _accepting: bool = field(default=False, init=False, repr=False)
    _in_flight: int = field(default=0, init=False, repr=False)
    _disposing: bool = field(default=False, init=False, repr=False)
    _disposed: bool = field(default=False, init=False, repr=False)
    _dispose_done: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )
    _quiescence_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _dispose_errors: tuple[Exception, ...] = field(default=(), init=False, repr=False)

    def as_dict(self) -> dict[str, ToolInfo]:
        return dict(self.tools)

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting and not self._disposed

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def disposed(self) -> bool:
        with self._lock:
            return self._disposed

    def activate(self) -> None:
        with self._lock:
            if self._disposing or self._disposed:
                raise RuntimeError(f"plugin generation {self.generation_id} is disposed")
            self._accepting = True

    def retire(self) -> None:
        """Stop future pins; already pinned invocations are allowed to finish."""
        with self._lock:
            self._accepting = False
            self._lock.notify_all()

    def add_disposer(self, disposer: Disposer) -> None:
        if not callable(disposer):
            raise TypeError("plugin disposer must be callable")
        with self._lock:
            if self._accepting or self._disposing or self._disposed:
                raise RuntimeError("plugin effects can only be registered while staging")
            if len(self._dispose_stack) >= MAX_DISPOSERS_PER_GENERATION:
                raise RuntimeError("plugin generation declares too many disposers")
            self._dispose_stack.append(disposer)

    def bind_tools(self, tools: dict[str, ToolInfo]) -> None:
        """Attach generation pins to validated staged tool objects exactly once."""
        if self.tools:
            raise RuntimeError("plugin generation tools are already bound")
        for tool in tools.values():
            execute = tool.execute

            async def pinned_execute(args: Any, ctx: Any, *, _execute=execute):
                self._pin()
                try:
                    return await _execute(args, ctx)
                finally:
                    self._unpin()

            # Diagnostics and tests can recover immutable generation identity
            # without trusting plugin-owned attributes.
            pinned_execute._openbox_plugin_generation = self.generation_id  # type: ignore[attr-defined]
            tool.execute = pinned_execute
        self.tools = tuple(tools.items())

    def _pin(self) -> None:
        with self._lock:
            if not self._accepting or self._disposed:
                raise PluginGenerationRetired(
                    f"platform plugin '{self.name}' generation is no longer active"
                )
            self._in_flight += 1

    def _unpin(self) -> None:
        waiters: tuple[tuple[asyncio.AbstractEventLoop, asyncio.Event], ...] = ()
        with self._lock:
            if self._in_flight <= 0:  # pragma: no cover - invariant guard
                raise RuntimeError("plugin generation pin underflow")
            self._in_flight -= 1
            if self._in_flight == 0:
                self._lock.notify_all()
                waiters = tuple(self._quiescence_waiters)
        # Tool invocations normally finish on their owning event loop, but the
        # lifecycle object is deliberately thread-safe.  Wake each async
        # waiter through its loop so quiescence never depends on availability
        # in asyncio's shared thread pool.
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # A loop can close while its reconcile task is being canceled;
                # the waiter removes itself in ``finally``.
                continue

    async def wait_quiescent(
        self,
        *,
        diagnostic_interval_seconds: float = QUIESCENCE_DIAGNOSTIC_SECONDS,
    ) -> None:
        interval = max(0.05, float(diagnostic_interval_seconds))
        loop = asyncio.get_running_loop()
        announced = False
        while True:
            event = asyncio.Event()
            waiter = (loop, event)
            with self._lock:
                if self._in_flight == 0:
                    return
                self._quiescence_waiters.add(waiter)
                in_flight = self._in_flight
            try:
                # Emit one deterministic diagnostic as soon as a retired
                # generation has to drain.  Subsequent messages remain paced
                # by ``interval``.  Besides making short-lived stalls visible,
                # this avoids coupling the first diagnostic to timer/thread
                # scheduling under a saturated process.
                if not announced:
                    log.warning(
                        "Waiting for platform plugin quiescence plugin=%s "
                        "generation=%s in_flight=%s",
                        self.name,
                        self.generation_id,
                        in_flight,
                    )
                    announced = True
                try:
                    await asyncio.wait_for(event.wait(), timeout=interval)
                except TimeoutError:
                    # Never unload code beneath a pinned call.  A swallowed
                    # cancellation is an availability incident, not permission
                    # to corrupt an active invocation; emit repeated diagnostics
                    # and continue draining.
                    if self.in_flight:
                        log.warning(
                            "Waiting for platform plugin quiescence plugin=%s "
                            "generation=%s in_flight=%s",
                            self.name,
                            self.generation_id,
                            self.in_flight,
                        )
            finally:
                with self._lock:
                    self._quiescence_waiters.discard(waiter)

    async def dispose(self) -> tuple[Exception, ...]:
        """Drain, release effects in reverse order, and clear owned modules.

        Every disposer gets a chance to run.  Failures are returned for
        diagnostics but never prevent later disposers or module cleanup.
        """
        self.retire()
        await self.wait_quiescent()
        with self._lock:
            if self._disposed:
                return self._dispose_errors
            if self._disposing:
                wait_for_owner = True
                stack: list[Disposer] = []
            else:
                wait_for_owner = False
                self._disposing = True
                stack = list(reversed(self._dispose_stack))
                self._dispose_stack.clear()

        if wait_for_owner:
            await asyncio.to_thread(self._dispose_done.wait)
            with self._lock:
                return self._dispose_errors

        errors: list[Exception] = []
        try:
            for disposer in stack:
                try:
                    await _maybe_await(disposer())
                except Exception as exc:  # one broken effect must not block its peers
                    errors.append(exc)
                    log.exception(
                        "Platform plugin disposer failed plugin=%s generation=%s",
                        self.name,
                        self.generation_id,
                        exc_info=exc,
                    )
        finally:
            try:
                for module_id, module in zip(self.module_ids, self.module_refs, strict=True):
                    if sys.modules.get(module_id) is module:
                        sys.modules.pop(module_id, None)
            finally:
                with self._lock:
                    self._dispose_errors = tuple(errors)
                    self._disposed = True
                    self._disposing = False
                    self._dispose_done.set()
        return tuple(errors)


class PluginLifecycleContext:
    """Bounded setup API exposed to a trusted plugin generation."""

    __slots__ = ("_generation", "dependencies")

    def __init__(
        self,
        generation: PluginGeneration,
        dependencies: dict[str, PluginGeneration],
    ) -> None:
        self._generation = generation
        self.dependencies = MappingProxyType(
            {
                name: PluginDependency(
                    name=dependency.name,
                    version=dependency.version,
                    generation_id=dependency.generation_id,
                    tool_ids=tuple(tool_id for tool_id, _tool in dependency.tools),
                )
                for name, dependency in dependencies.items()
            }
        )

    @property
    def plugin_name(self) -> str:
        return self._generation.name

    @property
    def version(self) -> str:
        return self._generation.version

    @property
    def generation_id(self) -> str:
        return self._generation.generation_id

    def add_disposer(self, disposer: Disposer) -> Disposer:
        """Bind a cleanup callback to this generation and return it."""
        self._generation.add_disposer(disposer)
        return disposer

    async def effect(self, setup: Callable[[], Any]) -> Any:
        """Run one setup effect immediately and own all returned disposers."""
        if not callable(setup):
            raise TypeError("plugin effect setup must be callable")
        result = await _maybe_await(setup())
        await register_dispose_result(self._generation, result)
        return result


async def register_dispose_result(generation: PluginGeneration, result: Any) -> None:
    """Normalize setup results into one bounded generation dispose stack."""
    result = await _maybe_await(result)
    if result is None:
        return
    if callable(result):
        generation.add_disposer(result)
        return
    if isinstance(result, AsyncIterable):
        count = 0
        async for disposer in result:
            generation.add_disposer(disposer)
            count += 1
            if count > MAX_DISPOSERS_PER_GENERATION:
                raise RuntimeError("plugin setup returned too many disposers")
        return
    if isinstance(result, Iterable) and not isinstance(result, (str, bytes, bytearray, dict)):
        for disposer in result:
            generation.add_disposer(disposer)
        return
    raise TypeError("plugin setup must return a disposer, iterable of disposers, or None")


async def invoke_lifecycle_hook(hook: Callable[..., Any], context: PluginLifecycleContext) -> Any:
    """Invoke a setup/activate hook accepting either ``()`` or ``(context)``."""
    try:
        signature = inspect.signature(hook)
    except (TypeError, ValueError):
        # Some extension callables do not expose a signature; the explicit
        # context form is the primary contract for them.
        args = (context,)
    else:
        try:
            signature.bind(context)
        except TypeError:
            try:
                signature.bind()
            except TypeError as exc:
                raise TypeError(
                    "plugin lifecycle hook must accept () or (PluginLifecycleContext)"
                ) from exc
            args = ()
        else:
            args = (context,)
    # Invocation happens only after arity selection, so a TypeError raised by
    # plugin code is never mistaken for a zero-argument signature fallback.
    returned = hook(*args)
    return await _maybe_await(returned)
