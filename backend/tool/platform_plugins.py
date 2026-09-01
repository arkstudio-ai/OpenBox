"""Trusted platform plugin discovery and transactional tool loading.

These plugins execute inside the backend process and therefore remain an
administrator-controlled extension plane, not a tenant code sandbox.  A
manifest gives each plugin a stable identity/version and lets one plugin's
tools be validated as a unit before the registry is changed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import itertools
import json
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from pydantic.errors import PydanticUserError

from tool.tool import ToolInfo, ToolResult

if TYPE_CHECKING:
    from tool.plugin_lifecycle import PluginGeneration


MANIFEST_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ENTRYPOINTS = 32
MAX_TOOLS_PER_PLUGIN = 128
MAX_DEPENDENCIES = 32
_PLUGIN_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_PLUGIN_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
_TOOL_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")
_MANIFEST_KEYS = frozenset(
    {"schema_version", "name", "version", "enabled", "entrypoints", "dependencies"}
)
_RESERVED_PLUGIN_NAMES = frozenset({"legacy-tools"})
_GENERATION_COUNTER = itertools.count(1)


class PlatformPluginError(ValueError):
    """A plugin cannot be loaded without weakening registry invariants."""


@dataclass(frozen=True)
class PlatformPluginManifest:
    name: str
    version: str
    directory: Path
    entrypoints: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    enabled: bool = True
    legacy: bool = False
    # Discovery records the lexical trust boundary so the loader can repeat
    # the symlink checks immediately before import.  This closes the gap where
    # an ancestor is exchanged after discovery but before an entrypoint opens.
    workspace_root: Path | None = None


def read_plugin_manifest(
    path: Path,
    *,
    workspace_root: Path | None = None,
) -> PlatformPluginManifest:
    """Read one bounded v1 manifest without importing its code."""
    path = path.absolute()
    boundary = workspace_root.absolute() if workspace_root is not None else path.parent
    if _path_contains_symlink(path, boundary) or (
        workspace_root is not None and _path_contains_symlink(boundary, Path(boundary.anchor))
    ):
        raise PlatformPluginError("plugin manifest, directory, and ancestors cannot be symlinks")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PlatformPluginError("plugin manifest is unreadable") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise PlatformPluginError("plugin manifest is too large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformPluginError("plugin manifest is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PlatformPluginError("plugin manifest must be an object")
    if set(payload) - _MANIFEST_KEYS:
        raise PlatformPluginError("plugin manifest contains unknown fields")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise PlatformPluginError("unsupported plugin manifest schema")

    name = payload.get("name")
    version = payload.get("version")
    enabled = payload.get("enabled", True)
    entrypoints = payload.get("entrypoints", ["tools.py"])
    dependencies = payload.get("dependencies", [])
    if not isinstance(name, str) or not _PLUGIN_NAME.fullmatch(name):
        raise PlatformPluginError("invalid plugin name")
    if name in _RESERVED_PLUGIN_NAMES:
        raise PlatformPluginError("plugin name is reserved")
    if path.parent.name != name:
        raise PlatformPluginError("plugin name must match its directory")
    if not isinstance(version, str) or not _PLUGIN_VERSION.fullmatch(version):
        raise PlatformPluginError("invalid plugin version")
    if not isinstance(enabled, bool):
        raise PlatformPluginError("plugin enabled must be boolean")
    if (
        not isinstance(entrypoints, list)
        or not entrypoints
        or len(entrypoints) > MAX_ENTRYPOINTS
        or any(not isinstance(item, str) or not item for item in entrypoints)
    ):
        raise PlatformPluginError("plugin entrypoints must be a bounded non-empty list")
    if len(set(entrypoints)) != len(entrypoints):
        raise PlatformPluginError("plugin entrypoints must be unique")
    if (
        not isinstance(dependencies, list)
        or len(dependencies) > MAX_DEPENDENCIES
        or any(not isinstance(item, str) or not _PLUGIN_NAME.fullmatch(item) for item in dependencies)
    ):
        raise PlatformPluginError("plugin dependencies must be a bounded list of plugin names")
    if len(set(dependencies)) != len(dependencies):
        raise PlatformPluginError("plugin dependencies must be unique")
    if name in dependencies:
        raise PlatformPluginError("plugin cannot depend on itself")
    return PlatformPluginManifest(
        name=name,
        version=version,
        directory=path.parent,
        entrypoints=tuple(entrypoints),
        dependencies=tuple(dependencies),
        enabled=enabled,
        workspace_root=boundary if workspace_root is not None else None,
    )


def discover_plugin_manifests(workspace_root: Path) -> list[PlatformPluginManifest]:
    """Discover modern manifests, plus the legacy host-tools directory."""
    workspace_root = workspace_root.absolute()
    if _path_contains_symlink(workspace_root, Path(workspace_root.anchor)):
        raise PlatformPluginError("workspace root and ancestors cannot be symlinks")
    manifests: list[PlatformPluginManifest] = []
    plugin_root = workspace_root / ".openbox" / "plugins"
    if plugin_root.exists() or plugin_root.is_symlink():
        if _path_contains_symlink(plugin_root, workspace_root):
            raise PlatformPluginError("platform plugin root and ancestors cannot be symlinks")
        if not plugin_root.is_dir():
            raise PlatformPluginError("platform plugin root must be a directory")
        try:
            resolved_root = plugin_root.resolve(strict=True)
        except OSError as exc:
            raise PlatformPluginError("platform plugin root is unreadable") from exc
        for path in sorted(plugin_root.glob("*/plugin.json")):
            if path.parent.resolve().parent != resolved_root:
                raise PlatformPluginError("plugin directory escapes the platform plugin root")
            manifests.append(read_plugin_manifest(path, workspace_root=workspace_root))

    legacy_dir = workspace_root / ".openbox" / "tools"
    if not (legacy_dir.exists() or legacy_dir.is_symlink()):
        legacy_dir = workspace_root / ".openagent" / "tools"
    if legacy_dir.exists() or legacy_dir.is_symlink():
        if _path_contains_symlink(legacy_dir, workspace_root):
            raise PlatformPluginError("legacy plugin directory and ancestors cannot be symlinks")
        if not legacy_dir.is_dir():
            raise PlatformPluginError("legacy plugin path must be a directory")
    legacy_files = tuple(path.name for path in sorted(legacy_dir.glob("*.py"))) if legacy_dir.is_dir() else ()
    if len(legacy_files) > MAX_ENTRYPOINTS:
        raise PlatformPluginError("legacy plugin declares too many entrypoints")
    if legacy_files:
        manifests.append(
            PlatformPluginManifest(
                name="legacy-tools",
                version="0",
                directory=legacy_dir,
                entrypoints=legacy_files,
                dependencies=(),
                legacy=True,
                workspace_root=workspace_root,
            ),
        )
    names = [manifest.name for manifest in manifests]
    if len(names) != len(set(names)):
        raise PlatformPluginError("platform plugin names must be unique")
    return manifests


def order_plugin_manifests(
    manifests: list[PlatformPluginManifest],
) -> list[PlatformPluginManifest]:
    """Return enabled plugins in deterministic dependency-first order.

    Missing/disabled dependencies and cycles reject the complete desired
    graph before any module is imported.  Stable discovery order is used for
    otherwise independent nodes, preserving modern-plugin priority over the
    legacy adapter while still honoring explicit dependency edges.
    """
    enabled = [manifest for manifest in manifests if manifest.enabled]
    by_name = {manifest.name: manifest for manifest in enabled}
    order_index = {manifest.name: index for index, manifest in enumerate(enabled)}
    for manifest in enabled:
        missing = [name for name in manifest.dependencies if name not in by_name]
        if missing:
            raise PlatformPluginError(
                f"plugin '{manifest.name}' has missing or disabled dependencies: "
                + ", ".join(sorted(missing))
            )

    indegree = {manifest.name: len(manifest.dependencies) for manifest in enabled}
    dependents: dict[str, list[str]] = {manifest.name: [] for manifest in enabled}
    for manifest in enabled:
        for dependency in manifest.dependencies:
            dependents[dependency].append(manifest.name)
    ready = sorted(
        (name for name, degree in indegree.items() if degree == 0),
        key=order_index.__getitem__,
    )
    result: list[PlatformPluginManifest] = []
    while ready:
        name = ready.pop(0)
        result.append(by_name[name])
        for dependent in sorted(dependents[name], key=order_index.__getitem__):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=order_index.__getitem__)
    if len(result) != len(enabled):
        cycle = sorted(name for name, degree in indegree.items() if degree)
        raise PlatformPluginError(
            "platform plugin dependency cycle: " + " -> ".join(cycle)
        )
    return result


def _path_contains_symlink(path: Path, stop: Path) -> bool:
    """Check a lexical path and every ancestor through the boundary itself."""
    current = path.absolute()
    boundary = stop.absolute()
    try:
        current.relative_to(boundary)
    except ValueError:
        return True
    while True:
        if current.is_symlink():
            return True
        if current == boundary:
            return False
        parent = current.parent
        if parent == current:
            return True
        current = parent


def _entrypoint_path(manifest: PlatformPluginManifest, entrypoint: str) -> Path:
    entrypoint_path = Path(entrypoint)
    if (
        entrypoint_path.name != entrypoint
        or "/" in entrypoint
        or "\\" in entrypoint
        or entrypoint in {".", ".."}
    ):
        raise PlatformPluginError("plugin entrypoint must be a direct Python filename")
    boundary = manifest.workspace_root or manifest.directory
    if _path_contains_symlink(manifest.directory, boundary) or (
        manifest.workspace_root is not None
        and _path_contains_symlink(boundary, Path(boundary.anchor))
    ):
        raise PlatformPluginError("plugin directory and ancestors cannot be symlinks")
    try:
        root = manifest.directory.resolve(strict=True)
    except OSError as exc:
        raise PlatformPluginError("plugin directory is unreadable") from exc
    candidate = manifest.directory / entrypoint
    if candidate.suffix != ".py" or candidate.is_symlink():
        raise PlatformPluginError("plugin entrypoint must be a regular Python file")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PlatformPluginError("plugin entrypoint does not exist") from exc
    if resolved.parent != root or not resolved.is_file():
        raise PlatformPluginError("plugin entrypoint escapes its plugin directory")
    return resolved


def _module_name(
    manifest: PlatformPluginManifest,
    path: Path,
    *,
    generation_id: str | None = None,
) -> str:
    identity = f"{path}\0{manifest.version}"
    if generation_id is not None:
        identity += f"\0{generation_id}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    safe_name = manifest.name.replace("-", "_")
    return f"_openbox_platform_plugin_{safe_name}_{digest}"


def platform_plugin_fingerprint(manifest: PlatformPluginManifest) -> tuple[str, str]:
    """Hash bounded manifest identity and entrypoint contents without import."""
    manifest_payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "name": manifest.name,
        "version": manifest.version,
        "enabled": manifest.enabled,
        "entrypoints": list(manifest.entrypoints),
        "dependencies": list(manifest.dependencies),
        "legacy": manifest.legacy,
    }
    manifest_digest = hashlib.sha256(
        json.dumps(
            manifest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    snapshots: list[tuple[str, bytes]] = []
    for entrypoint in manifest.entrypoints:
        path = _entrypoint_path(manifest, entrypoint)
        try:
            source = path.read_bytes()
        except OSError as exc:
            raise PlatformPluginError("plugin entrypoint is unreadable") from exc
        snapshots.append((entrypoint, source))
    return manifest_digest, _source_snapshot_digest(manifest_digest, snapshots)


def _source_snapshot_digest(
    manifest_digest: str,
    snapshots: list[tuple[str, bytes]],
) -> str:
    source_hash = hashlib.sha256()
    source_hash.update(manifest_digest.encode("ascii"))
    for entrypoint, source in snapshots:
        source_hash.update(entrypoint.encode("utf-8"))
        source_hash.update(b"\0")
        source_hash.update(source)
        source_hash.update(b"\0")
    return source_hash.hexdigest()


class _ValidatedArguments(dict[str, Any]):
    """Validated legacy mapping that also exposes model-style attributes."""

    def __init__(self, model: BaseModel):
        super().__init__(model.model_dump())
        self._model = model

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)


def _validated_execute(tool: ToolInfo, *, typed_arguments: bool):
    """Adapt the public dict call shape to typed and legacy implementations."""
    original_execute = tool.execute
    parameter_model = tool.parameters
    tool_id = tool.id

    async def execute(args: dict[str, Any] | BaseModel, ctx: Any) -> ToolResult:
        try:
            validated = parameter_model.model_validate(args)
        except Exception as exc:
            return ToolResult(
                title=f"Invalid input for {tool_id}",
                output=f"Parameter validation error: {exc}",
            )
        call_args: BaseModel | dict[str, Any]
        if typed_arguments:
            call_args = validated
        else:
            # Historical .openbox/tools exports received dictionaries.  Keep
            # that contract, but pass validated/default-populated data rather
            # than the untrusted JSON object supplied by the model.  Attribute
            # delegation also gives unannotated typed-style legacy functions a
            # safe compatibility path instead of a runtime args.value crash.
            call_args = _ValidatedArguments(validated)
        result = await original_execute(call_args, ctx)
        if not isinstance(result, ToolResult):
            raise TypeError(f"Platform plugin tool '{tool_id}' must return ToolResult")
        return result

    return execute


def _normalize_tool(tool: ToolInfo) -> None:
    if not _TOOL_ID.fullmatch(tool.id):
        raise PlatformPluginError("plugin tool id is invalid")
    try:
        valid_parameters = issubclass(tool.parameters, BaseModel)
    except TypeError:
        valid_parameters = False
    if not valid_parameters or not callable(tool.execute) or not inspect.iscoroutinefunction(tool.execute):
        raise PlatformPluginError("plugin tool contract is invalid")
    try:
        tool.parameters.model_json_schema()
        signature = inspect.signature(tool.execute)
        signature.bind({}, None)
        first_parameter = next(iter(signature.parameters.values()))
        try:
            annotation = inspect.get_annotations(tool.execute, eval_str=True).get(
                first_parameter.name,
                inspect.Signature.empty,
            )
        except (NameError, TypeError, ValueError) as exc:
            raise TypeError("tool argument annotation cannot be resolved") from exc
        typed_arguments = False
        if isinstance(annotation, type):
            try:
                annotation_is_model = issubclass(annotation, BaseModel)
            except TypeError:
                annotation_is_model = False
            if annotation_is_model:
                if annotation is not tool.parameters:
                    raise TypeError("tool argument annotation does not match parameters")
                typed_arguments = True
        if tool.raw_schema is not None:
            if not isinstance(tool.raw_schema, dict):
                raise TypeError("raw schema must be an object")
            json.dumps(tool.raw_schema)
    except (TypeError, ValueError, PydanticUserError) as exc:
        raise PlatformPluginError("plugin tool contract is invalid") from exc
    # Trust metadata belongs to the registration channel, never plugin code.
    tool.source = "custom"
    tool.plane = "platform"
    tool.canonical_id = tool.id
    tool.provider_name = tool.id
    tool.pack = None
    tool.same_response_safe = False
    tool.parallel_safe = False
    # Processor/batch dispatchers pass JSON objects, while typed implementations
    # receive the declared Pydantic model.  Unannotated/dict executors retain
    # the legacy mapping contract, so both ``args.value`` and ``args["value"]``
    # remain valid for their declared form.
    tool.execute = _validated_execute(tool, typed_arguments=typed_arguments)


def load_platform_plugin(
    manifest: PlatformPluginManifest,
    *,
    reserved_ids: set[str] | frozenset[str],
) -> dict[str, ToolInfo]:
    """Import and validate one plugin atomically, returning staged tools."""
    if not manifest.enabled:
        return {}
    # The loader is also safe when used directly in tests or startup helpers:
    # imported modules may export ToolInfo objects, but cannot mutate the live
    # registry through register() before the complete plugin has validated.
    from tool.registry import platform_plugin_import_guard

    staged: dict[str, ToolInfo] = {}
    inserted_modules: list[str] = []
    seen_objects: set[int] = set()
    try:
        with platform_plugin_import_guard():
            for entrypoint in manifest.entrypoints:
                path = _entrypoint_path(manifest, entrypoint)
                module_name = _module_name(manifest, path)
                module = sys.modules.get(module_name)
                if module is None:
                    spec = importlib.util.spec_from_file_location(module_name, path)
                    if spec is None or spec.loader is None:
                        raise PlatformPluginError("plugin module cannot be loaded")
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    inserted_modules.append(module_name)
                    spec.loader.exec_module(module)

                for attr_name in dir(module):
                    value = getattr(module, attr_name, None)
                    if not isinstance(value, ToolInfo) or id(value) in seen_objects:
                        continue
                    seen_objects.add(id(value))
                    if value.id in reserved_ids or value.id in staged:
                        raise PlatformPluginError("plugin tool id collides with the registry")
                    # A plugin may import a built-in ToolInfo into its module. Never
                    # mutate that shared object while applying registration-owned
                    # trust metadata; stage a shallow dataclass copy instead.
                    candidate = replace(value)
                    _normalize_tool(candidate)
                    staged[candidate.id] = candidate
                    if len(staged) > MAX_TOOLS_PER_PLUGIN:
                        raise PlatformPluginError("plugin declares too many tools")
    except BaseException:
        for module_name in inserted_modules:
            sys.modules.pop(module_name, None)
        raise
    if not staged:
        for module_name in inserted_modules:
            sys.modules.pop(module_name, None)
        raise PlatformPluginError("plugin does not export any tools")
    return staged


def _module_hook(module: Any, *names: str) -> Any | None:
    """Resolve an explicitly module-owned lifecycle hook."""
    namespace = vars(module)
    for name in names:
        hook = namespace.get(name)
        if hook is None:
            continue
        if not callable(hook):
            raise PlatformPluginError(f"plugin lifecycle hook '{name}' must be callable")
        # Ignore a generic imported helper named setup/dispose.  Explicit
        # ``openbox_*`` aliases are always intentional; conventional names
        # must be functions/classes defined by this entrypoint.
        if name.startswith("openbox_") or getattr(hook, "__module__", None) == module.__name__:
            return hook
    return None


async def stage_platform_plugin_generation(
    manifest: PlatformPluginManifest,
    *,
    reserved_ids: set[str] | frozenset[str],
    dependencies: dict[str, "PluginGeneration"] | None = None,
) -> "PluginGeneration":
    """Import, validate and set up one invisible plugin generation.

    Nothing is published to the live registry here.  Every imported module and
    setup effect is attached to the returned generation; any failure drains
    that partial generation before the exception escapes.
    """
    from tool.plugin_lifecycle import (
        PluginGeneration,
        PluginLifecycleContext,
        invoke_lifecycle_hook,
        register_dispose_result,
    )
    from tool.registry import platform_plugin_import_guard

    if not manifest.enabled:
        raise PlatformPluginError("disabled plugins cannot be staged")
    dependency_generations = dependencies or {}
    missing_dependencies = [
        name for name in manifest.dependencies if name not in dependency_generations
    ]
    if missing_dependencies:
        raise PlatformPluginError(
            "plugin dependency generation is unavailable: "
            + ", ".join(missing_dependencies)
        )
    manifest_digest, source_digest = platform_plugin_fingerprint(manifest)
    serial = next(_GENERATION_COUNTER)
    generation_id = (
        f"{manifest.name}@{manifest.version}:"
        f"{source_digest[:16]}:{serial}"
    )
    generation = PluginGeneration(
        name=manifest.name,
        version=manifest.version,
        entrypoints=manifest.entrypoints,
        legacy=manifest.legacy,
        dependencies=manifest.dependencies,
        dependency_generation_ids=tuple(
            (name, dependency_generations[name].generation_id)
            for name in manifest.dependencies
        ),
        manifest_digest=manifest_digest,
        source_digest=source_digest,
        generation_id=generation_id,
    )
    staged: dict[str, ToolInfo] = {}
    modules: list[Any] = []
    module_ids: list[str] = []
    seen_objects: set[int] = set()
    try:
        source_snapshots: list[tuple[str, Path, bytes]] = []
        for entrypoint in manifest.entrypoints:
            path = _entrypoint_path(manifest, entrypoint)
            try:
                source = path.read_bytes()
            except OSError as exc:
                raise PlatformPluginError("plugin entrypoint is unreadable") from exc
            source_snapshots.append((entrypoint, path, source))
        if _source_snapshot_digest(
            manifest_digest,
            [(entrypoint, source) for entrypoint, _path, source in source_snapshots],
        ) != source_digest:
            raise PlatformPluginError("plugin source changed while staging")

        with platform_plugin_import_guard():
            for entrypoint, path, source in source_snapshots:
                module_name = _module_name(
                    manifest,
                    path,
                    generation_id=generation_id,
                )
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise PlatformPluginError("plugin module cannot be loaded")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                module_ids.append(module_name)
                modules.append(module)
                # Execute the exact bytes covered by source_digest.  The
                # standard SourceFileLoader may reuse timestamp-based pyc for
                # a same-size edit within one second, which is unacceptable
                # for deterministic in-process hot replacement.
                code = compile(source, str(path), "exec", dont_inherit=True)
                exec(code, module.__dict__)

                for value in vars(module).values():
                    if not isinstance(value, ToolInfo) or id(value) in seen_objects:
                        continue
                    seen_objects.add(id(value))
                    if value.id in reserved_ids or value.id in staged:
                        raise PlatformPluginError("plugin tool id collides with the registry")
                    candidate = replace(value)
                    _normalize_tool(candidate)
                    staged[candidate.id] = candidate
                    if len(staged) > MAX_TOOLS_PER_PLUGIN:
                        raise PlatformPluginError("plugin declares too many tools")

        generation.module_ids = tuple(module_ids)
        generation.module_refs = tuple(modules)
        if not staged:
            raise PlatformPluginError("plugin does not export any tools")
        generation.bind_tools(staged)

        lifecycle_context = PluginLifecycleContext(
            generation,
            {
                name: dependency_generations[name]
                for name in manifest.dependencies
                if name in dependency_generations
            },
        )
        with platform_plugin_import_guard():
            for module in modules:
                setup = _module_hook(module, "openbox_setup", "setup")
                activate = _module_hook(module, "openbox_activate", "activate")
                if setup is not None and activate is not None:
                    raise PlatformPluginError(
                        "plugin entrypoint must declare only one setup/activate hook"
                    )
                dispose = _module_hook(module, "openbox_dispose", "dispose")
                # Register module teardown before setup effects.  Reverse disposal
                # then releases setup-owned effects before the module-wide hook.
                if dispose is not None:
                    generation.add_disposer(
                        lambda _dispose=dispose: invoke_lifecycle_hook(
                            _dispose,
                            lifecycle_context,
                        )
                    )
                hook = setup or activate
                if hook is not None:
                    result = await invoke_lifecycle_hook(hook, lifecycle_context)
                    await register_dispose_result(generation, result)
        final_manifest_digest, final_source_digest = platform_plugin_fingerprint(manifest)
        if (
            final_manifest_digest != manifest_digest
            or final_source_digest != source_digest
        ):
            raise PlatformPluginError("plugin source changed while setup was running")
        if not manifest.legacy:
            current_manifest = read_plugin_manifest(
                manifest.directory / "plugin.json",
                workspace_root=manifest.workspace_root,
            )
            if current_manifest != manifest:
                raise PlatformPluginError("plugin manifest changed while setup was running")
        return generation
    except BaseException:
        # Assign ownership even when import failed part-way through, otherwise
        # the abort path could not remove the inserted modules.
        generation.module_ids = tuple(module_ids)
        generation.module_refs = tuple(modules)
        with platform_plugin_import_guard():
            await generation.dispose()
        raise
