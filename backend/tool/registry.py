"""Tool registry: manages all available tools."""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from tool.plugin_lifecycle import PluginGeneration
from tool.tool import ToolInfo
from core.log import create_logger

log = create_logger("tool.registry")

# Global tool registry
_tools: dict[str, ToolInfo] = {}
_loading_platform_plugin: ContextVar[int] = ContextVar(
    "openbox_loading_platform_plugin",
    default=0,
)
_loaded_platform_plugins: dict[str, PluginGeneration] = {}
_registry_pointer_lock = threading.RLock()


class _AsyncThreadLock:
    """Loop-independent async lock used by sync compatibility bridges too."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @property
    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> None:
        acquire = asyncio.create_task(asyncio.to_thread(self._lock.acquire))
        try:
            await asyncio.shield(acquire)
        except asyncio.CancelledError:
            # ``to_thread`` cannot cancel a blocking Lock.acquire.  Join it and
            # release the ownership it eventually obtained, otherwise one
            # cancelled reconcile would wedge every future reload.
            await acquire
            self._lock.release()
            raise

    async def __aexit__(self, *_exc: Any) -> None:
        self._lock.release()


_platform_reconcile_lock = _AsyncThreadLock()


def register(tool: ToolInfo) -> None:
    """Register a tool."""
    if _loading_platform_plugin.get():
        raise RuntimeError("platform plugins must export ToolInfo instead of registering during import")
    global _tools
    with _registry_pointer_lock:
        existing = _tools.get(tool.id)
        if existing is not None and existing is not tool:
            raise ValueError(f"Tool id is already registered: {tool.id}")
        # Copy-on-write makes the live dictionary itself a CAS token for
        # plugin reconciliation; readers still need no lock.
        _tools = {**_tools, tool.id: tool}
    log.debug(f"Registered tool: {tool.id}")


@contextmanager
def platform_plugin_import_guard() -> Iterator[None]:
    """Reject live registry side effects during plugin import/setup.

    Context-local state survives awaits and is inherited by plugin-created
    tasks without blocking unrelated trusted registrations in other tasks.
    """
    token = _loading_platform_plugin.set(_loading_platform_plugin.get() + 1)
    try:
        yield
    finally:
        _loading_platform_plugin.reset(token)


def get_tool(tool_id: str) -> ToolInfo | None:
    """Get a tool by ID."""
    return _tools.get(tool_id)


def list_tools() -> list[ToolInfo]:
    """List all registered tools."""
    return list(_tools.values())


def get_tools_for_agent(tool_ids: list[str]) -> dict[str, ToolInfo]:
    """Get tools filtered by a list of tool IDs."""
    return {tid: _tools[tid] for tid in tool_ids if tid in _tools}


def register_builtin_tools(*, load_custom: bool = True) -> None:
    """Register all built-in tools."""
    from tool.bash import bash_tool
    from tool.read import read_tool
    from tool.write import write_tool
    from tool.edit import edit_tool
    from tool.apply_patch import apply_patch_tool
    from tool.glob_tool import glob_tool
    from tool.grep import grep_tool
    from tool.task import task_tool
    from tool.batch import batch_tool
    from tool.question_tool import question_tool
    from tool.todo_tool import todo_write_tool, todo_read_tool
    from tool.plan import plan_enter_tool, plan_exit_tool
    from tool.skill_tool import skill_search_tool, skill_tool
    from tool.web_fetch import web_fetch_tool
    from tool.web_search import web_search_tool
    from tool.invalid import invalid_tool
    from tool.multiedit import multiedit_tool
    from tool.cron_tool import cron_tool
    from tool.view_image import view_image_tool
    from tool.share_file import share_file_tool
    from tool.image_gen import image_gen_tool
    from tool.computer import computer_tool
    from tool.browser_mode import browser_mode_tool
    from tool.skill_manage import skill_manage_tool
    from tool.creator_context import creator_context_tool
    from tool.capability_search import capability_search_tool
    from tool.video_production import video_generate_tool, video_transcribe_tool

    for tool in [
        bash_tool, read_tool, write_tool, edit_tool, apply_patch_tool,
        glob_tool, grep_tool, task_tool, batch_tool, question_tool,
        todo_write_tool, todo_read_tool, plan_enter_tool, plan_exit_tool,
        skill_tool, skill_search_tool, web_fetch_tool, web_search_tool, invalid_tool,
        multiedit_tool, cron_tool, view_image_tool, share_file_tool, image_gen_tool,
        video_generate_tool, video_transcribe_tool,
        computer_tool, browser_mode_tool, skill_manage_tool,
        creator_context_tool, capability_search_tool,
    ]:
        register(tool)

    log.info(f"Registered {len(_tools)} built-in tools")

    # Load custom tools from .openbox/tools/*.py (fallback .openagent/tools/)
    if load_custom:
        register_custom_tools()


async def _dispose_generations(generations: list[PluginGeneration]) -> None:
    """Dispose generations independently so one broken cleanup cannot leak peers."""
    for generation in generations:
        with platform_plugin_import_guard():
            errors = await generation.dispose()
        if errors:
            log.warning(
                "Platform plugin '%s' disposed with %s cleanup error(s)",
                generation.name,
                len(errors),
            )


async def _dispose_generations_before_cancelling(
    generations: list[PluginGeneration],
) -> None:
    """Do not orphan retired code if the reconcile caller is cancelled."""
    if not generations:
        return
    task = asyncio.create_task(_dispose_generations(generations))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # The registry pointer may already exclude these generations.  Finish
        # ownership cleanup before propagating cancellation to the caller.
        await task
        raise


async def reconcile_platform_plugins(
    workspace_root: Path | None = None,
) -> bool:
    """Transactionally reconcile trusted plugins against the live registry.

    Import, validation and asynchronous setup all finish before one CAS pointer
    swap publishes the new tool set.  Failed replacements preserve an intact,
    collision-free last-known-good generation.  Retired generations reject
    new pins, drain existing calls, then release effects/modules in reverse
    dependency/activation order.
    """
    global _tools, _loaded_platform_plugins

    from tool.platform_plugins import (
        PlatformPluginError,
        discover_plugin_manifests,
        order_plugin_manifests,
        platform_plugin_fingerprint,
        stage_platform_plugin_generation,
    )

    root = (workspace_root or Path.cwd()).absolute()
    async with _platform_reconcile_lock:
        try:
            manifests = discover_plugin_manifests(root)
            ordered = order_plugin_manifests(manifests)
        except PlatformPluginError:
            log.error(
                "Invalid platform plugin graph; retained last-known-good generations",
                exc_info=True,
            )
            return False

        # Built-in registration is copy-on-write.  A rare concurrent register
        # invalidates this snapshot and causes a clean restage/retry.
        for _attempt in range(3):
            with _registry_pointer_lock:
                expected_tools = _tools
                expected_loaded = _loaded_platform_plugins

            owned_live: dict[str, ToolInfo] = {}
            intact_by_name: dict[str, bool] = {}
            for name, generation in expected_loaded.items():
                intact = all(
                    expected_tools.get(tool_id) is tool
                    for tool_id, tool in generation.tools
                )
                intact_by_name[name] = intact
                for tool_id, tool in generation.tools:
                    if expected_tools.get(tool_id) is tool:
                        owned_live[tool_id] = tool
            base_tools = {
                tool_id: tool
                for tool_id, tool in expected_tools.items()
                if owned_live.get(tool_id) is not tool
            }
            reserved_ids = set(base_tools)
            next_loaded: dict[str, PluginGeneration] = {}
            staged_new: list[PluginGeneration] = []
            desired_names = {manifest.name for manifest in ordered}
            can_retain_complete_lkg = (
                set(expected_loaded).issubset(desired_names)
                and all(intact_by_name.values())
            )
            retain_complete_lkg = False

            for manifest in ordered:
                owned = expected_loaded.get(manifest.name)
                selected: PluginGeneration | None = None
                try:
                    manifest_digest, source_digest = platform_plugin_fingerprint(manifest)
                except Exception:
                    log.warning(
                        "Failed to fingerprint platform plugin '%s'",
                        manifest.name,
                        exc_info=True,
                    )
                    manifest_digest = source_digest = ""

                if (
                    owned is not None
                    and intact_by_name.get(manifest.name, False)
                    and owned.manifest_digest == manifest_digest
                    and owned.source_digest == source_digest
                    and owned.dependencies == manifest.dependencies
                    and owned.dependency_generation_ids == tuple(
                        (
                            dependency,
                            next_loaded[dependency].generation_id,
                        )
                        for dependency in manifest.dependencies
                        if dependency in next_loaded
                    )
                    and all(
                        dependency in next_loaded
                        for dependency in manifest.dependencies
                    )
                    and not reserved_ids.intersection(owned.as_dict())
                ):
                    selected = owned
                else:
                    missing_runtime_dependencies = [
                        dependency
                        for dependency in manifest.dependencies
                        if dependency not in next_loaded
                    ]
                    try:
                        if missing_runtime_dependencies:
                            raise PlatformPluginError(
                                "dependency failed to activate: "
                                + ", ".join(missing_runtime_dependencies)
                            )
                        selected = await stage_platform_plugin_generation(
                            manifest,
                            reserved_ids=reserved_ids,
                            dependencies=next_loaded,
                        )
                    except Exception:
                        log.warning(
                            "Failed to stage platform plugin '%s'",
                            manifest.name,
                            exc_info=True,
                        )
                        owned_tools = owned.as_dict() if owned is not None else {}
                        if (
                            owned is not None
                            and can_retain_complete_lkg
                            and not reserved_ids.intersection(owned_tools)
                        ):
                            # Dependency generations form one ownership graph.
                            # If a replacement inside that graph fails, keep the
                            # complete previously committed graph instead of
                            # publishing a new provider while silently dropping
                            # its dependent's LKG.
                            retain_complete_lkg = True
                            log.warning(
                                "Retained complete last-known-good platform plugin graph after '%s' failed",
                                manifest.name,
                            )
                            break
                        if (
                            owned is not None
                            and intact_by_name.get(manifest.name, False)
                            and not reserved_ids.intersection(owned_tools)
                            and all(
                                dependency in next_loaded
                                for dependency in owned.dependencies
                            )
                            and owned.dependency_generation_ids == tuple(
                                (
                                    dependency,
                                    next_loaded[dependency].generation_id,
                                )
                                for dependency in owned.dependencies
                            )
                        ):
                            selected = owned
                            log.warning(
                                "Retained last-known-good platform plugin '%s'",
                                manifest.name,
                            )
                        else:
                            selected = None
                    else:
                        staged_new.append(selected)

                if selected is None:
                    continue
                selected_tools = selected.as_dict()
                collisions = reserved_ids.intersection(selected_tools)
                if collisions:
                    staged_match = next(
                        (item for item in staged_new if item is selected),
                        None,
                    )
                    if staged_match is not None:
                        staged_new = [
                            item for item in staged_new if item is not staged_match
                        ]
                        await _dispose_generations_before_cancelling([selected])
                    log.warning(
                        "Platform plugin '%s' conflicts with higher-priority tools: %s",
                        manifest.name,
                        ", ".join(sorted(collisions)),
                    )
                    continue
                next_loaded[manifest.name] = selected
                reserved_ids.update(selected_tools)

            if retain_complete_lkg:
                await _dispose_generations_before_cancelling(
                    list(reversed(staged_new))
                )
                return True

            next_plugin_tools = {
                tool_id: tool
                for generation in next_loaded.values()
                for tool_id, tool in generation.tools
            }
            next_tools = {**base_tools, **next_plugin_tools}
            next_generation_objects = {id(item) for item in next_loaded.values()}
            retiring = [
                generation
                for generation in reversed(tuple(expected_loaded.values()))
                if id(generation) not in next_generation_objects
            ]

            with _registry_pointer_lock:
                if _tools is not expected_tools or _loaded_platform_plugins is not expected_loaded:
                    cas_succeeded = False
                else:
                    # Retire-before-swap closes the small window in which a
                    # stale resolved ToolInfo could begin after removal.
                    for generation in retiring:
                        generation.retire()
                    previous_generation_objects = {
                        id(item) for item in expected_loaded.values()
                    }
                    for generation in next_loaded.values():
                        if id(generation) not in previous_generation_objects:
                            generation.activate()
                    _tools = next_tools
                    _loaded_platform_plugins = next_loaded
                    cas_succeeded = True

            if not cas_succeeded:
                await _dispose_generations_before_cancelling(
                    list(reversed(staged_new))
                )
                continue

            await _dispose_generations_before_cancelling(retiring)
            removed_names = set(expected_loaded) - set(next_loaded)
            for removed_name in sorted(removed_names):
                log.info("Unloaded platform plugin '%s'", removed_name)
            for name, generation in next_loaded.items():
                log.info(
                    "Active platform plugin '%s' version=%s generation=%s tools=%s legacy=%s",
                    name,
                    generation.version,
                    generation.generation_id,
                    len(generation.tools),
                    generation.legacy,
                )
            log.info(
                "Platform plugin reconcile complete: %s generation(s), %s tool(s)",
                len(next_loaded),
                len(next_plugin_tools),
            )
            return True

        log.error("Platform plugin registry CAS failed after three retries")
        return False


async def shutdown_platform_plugins() -> None:
    """Atomically unpublish, drain and dispose every platform generation."""
    global _tools, _loaded_platform_plugins
    async with _platform_reconcile_lock:
        with _registry_pointer_lock:
            current_loaded = _loaded_platform_plugins
            owned = {
                tool_id: tool
                for generation in current_loaded.values()
                for tool_id, tool in generation.tools
                if _tools.get(tool_id) is tool
            }
            for generation in current_loaded.values():
                generation.retire()
            _tools = {
                tool_id: tool
                for tool_id, tool in _tools.items()
                if owned.get(tool_id) is not tool
            }
            _loaded_platform_plugins = {}
        await _dispose_generations_before_cancelling(
            list(reversed(tuple(current_loaded.values())))
        )


def _run_reconcile_compatibly() -> bool:
    """Bridge the historical synchronous startup/test API.

    Production lifespan uses the native async API.  If a legacy caller invokes
    this function from an already-running event loop, a short-lived helper
    thread owns reconciliation so the caller still observes completion before
    return.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(reconcile_platform_plugins())

    watcher = globals().get("platform_plugin_watcher")
    if _platform_reconcile_lock.locked or (
        watcher is not None and watcher.running
    ):
        raise RuntimeError(
            "register_custom_tools() cannot block an active async plugin lifecycle; "
            "await reconcile_platform_plugins() instead"
        )

    result: list[bool] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(reconcile_platform_plugins()))
        except BaseException as exc:  # propagate bridge/runtime defects
            error.append(exc)

    worker = threading.Thread(target=run, name="openbox-plugin-reconcile", daemon=False)
    worker.start()
    worker.join()
    if error:
        raise error[0]
    return result[0]


def register_custom_tools() -> None:
    """Compatibility wrapper for explicit synchronous plugin reconciliation."""
    _run_reconcile_compatibly()


class PlatformPluginWatcher:
    """Low-frequency production trigger for explicit generation reconcile."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._workspace_root: Path | None = None
        self._interval_seconds = 5.0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(
        self,
        *,
        workspace_root: Path | None = None,
        interval_seconds: float = 5.0,
    ) -> None:
        if self.running:
            return
        if interval_seconds < 0.25 or interval_seconds > 3_600:
            raise ValueError("platform plugin watch interval must be between 0.25 and 3600 seconds")
        self._workspace_root = (workspace_root or Path.cwd()).absolute()
        self._interval_seconds = float(interval_seconds)
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(),
            name="platform-plugin-watcher",
        )
        log.info(
            "Platform plugin watcher started interval_seconds=%s root=%s",
            self._interval_seconds,
            self._workspace_root,
        )

    async def stop(self) -> None:
        task = self._task
        stop = self._stop
        if task is None:
            return
        if stop is not None:
            stop.set()
        try:
            await task
        finally:
            self._task = None
            self._stop = None
            self._workspace_root = None
        log.info("Platform plugin watcher stopped")

    async def _run(self) -> None:
        assert self._stop is not None
        assert self._workspace_root is not None
        while True:
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval_seconds,
                )
                return
            except TimeoutError:
                pass

            try:
                reconciled = await reconcile_platform_plugins(self._workspace_root)
                if not reconciled:
                    log.warning(
                        "Platform plugin watcher retained last-known-good graph after invalid desired state"
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # One broken scan/reconcile must not terminate the production
                # trigger.  The live pointer remains the last committed graph.
                log.exception(
                    "Platform plugin watcher reconcile failed; retained last-known-good graph"
                )


platform_plugin_watcher = PlatformPluginWatcher()
