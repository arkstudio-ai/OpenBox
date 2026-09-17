import asyncio
import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tool.platform_plugins import (
    PlatformPluginError,
    discover_plugin_manifests,
    order_plugin_manifests,
)
from tool.plugin_lifecycle import PluginGenerationRetired


def _write_manifest(
    root: Path,
    name: str,
    *,
    version: str = "1.0.0",
    dependencies: tuple[str, ...] = (),
    enabled: bool = True,
    entrypoints: tuple[str, ...] = ("tools.py",),
) -> Path:
    directory = root / ".openbox" / "plugins" / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plugin.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": name,
                "version": version,
                "enabled": enabled,
                "entrypoints": list(entrypoints),
                "dependencies": list(dependencies),
            }
        ),
        encoding="utf-8",
    )
    return path


def _tool_source(
    tool_id: str,
    *,
    title: str,
    lifecycle: str = "",
    execute_body: str | None = None,
) -> str:
    body = execute_body or f'return ToolResult(title="{title}", output=args.value)'
    return f'''
from pydantic import BaseModel
from tool.tool import ToolInfo, ToolResult

class Input(BaseModel):
    value: str = "ok"

async def run(args: Input, ctx):
    {body}

plugin_tool = ToolInfo(
    id="{tool_id}",
    description="lifecycle fixture",
    parameters=Input,
    execute=run,
)

{lifecycle}
'''


async def _eventually(predicate, *, timeout_seconds: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not converge before timeout")


@pytest.fixture
async def isolated_registry(monkeypatch):
    from tool import registry

    await registry.platform_plugin_watcher.stop()
    monkeypatch.setattr(registry, "_tools", {"bash": object()})
    monkeypatch.setattr(registry, "_loaded_platform_plugins", {})
    yield registry
    await registry.shutdown_platform_plugins()


@pytest.mark.asyncio
async def test_dependency_topology_and_reverse_generation_disposal(
    tmp_path: Path,
    isolated_registry,
):
    marker = tmp_path / "order.txt"
    provider_manifest = _write_manifest(tmp_path, "z-provider")
    consumer_manifest = _write_manifest(
        tmp_path,
        "a-consumer",
        dependencies=("z-provider",),
    )
    provider_lifecycle = f'''
from pathlib import Path
MARKER = Path({str(marker)!r})
async def setup(ctx):
    MARKER.write_text((MARKER.read_text() if MARKER.exists() else "") + "setup:provider\\n")
async def dispose(ctx):
    MARKER.write_text(MARKER.read_text() + "dispose:provider\\n")
'''
    consumer_lifecycle = f'''
from pathlib import Path
MARKER = Path({str(marker)!r})
async def setup(ctx):
    assert tuple(ctx.dependencies) == ("z-provider",)
    MARKER.write_text(MARKER.read_text() + "setup:consumer\\n")
async def dispose(ctx):
    MARKER.write_text(MARKER.read_text() + "dispose:consumer\\n")
'''
    (provider_manifest.parent / "tools.py").write_text(
        _tool_source("provider_tool", title="provider", lifecycle=provider_lifecycle),
        encoding="utf-8",
    )
    (consumer_manifest.parent / "tools.py").write_text(
        _tool_source("consumer_tool", title="consumer", lifecycle=consumer_lifecycle),
        encoding="utf-8",
    )

    manifests = discover_plugin_manifests(tmp_path)
    assert [item.name for item in manifests] == ["a-consumer", "z-provider"]
    assert [item.name for item in order_plugin_manifests(manifests)] == [
        "z-provider",
        "a-consumer",
    ]
    assert await isolated_registry.reconcile_platform_plugins(tmp_path) is True
    assert marker.read_text(encoding="utf-8").splitlines() == [
        "setup:provider",
        "setup:consumer",
    ]
    old_provider = isolated_registry._loaded_platform_plugins["z-provider"]
    old_consumer = isolated_registry._loaded_platform_plugins["a-consumer"]

    # A provider epoch change rebuilds declared dependents against the new
    # immutable dependency identity before either old generation is disposed.
    payload = json.loads(provider_manifest.read_text(encoding="utf-8"))
    payload["version"] = "2.0.0"
    provider_manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert await isolated_registry.reconcile_platform_plugins(tmp_path) is True
    assert isolated_registry._loaded_platform_plugins["z-provider"] is not old_provider
    assert isolated_registry._loaded_platform_plugins["a-consumer"] is not old_consumer
    assert marker.read_text(encoding="utf-8").splitlines() == [
        "setup:provider",
        "setup:consumer",
        "setup:provider",
        "setup:consumer",
        "dispose:consumer",
        "dispose:provider",
    ]

    await isolated_registry.shutdown_platform_plugins()
    assert marker.read_text(encoding="utf-8").splitlines() == [
        "setup:provider",
        "setup:consumer",
        "setup:provider",
        "setup:consumer",
        "dispose:consumer",
        "dispose:provider",
        "dispose:consumer",
        "dispose:provider",
    ]


def test_dependency_cycle_and_missing_dependency_are_rejected_before_import(tmp_path: Path):
    first = _write_manifest(tmp_path, "first", dependencies=("second",))
    second = _write_manifest(tmp_path, "second", dependencies=("first",))
    (first.parent / "tools.py").write_text("raise AssertionError('must not import')")
    (second.parent / "tools.py").write_text("raise AssertionError('must not import')")

    with pytest.raises(PlatformPluginError, match="cycle"):
        order_plugin_manifests(discover_plugin_manifests(tmp_path))

    payload = json.loads(second.read_text(encoding="utf-8"))
    payload["dependencies"] = ["absent"]
    second.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PlatformPluginError, match="missing or disabled"):
        order_plugin_manifests(discover_plugin_manifests(tmp_path))


@pytest.mark.asyncio
async def test_setup_failure_cleans_staged_modules_and_preserves_lkg(
    tmp_path: Path,
    isolated_registry,
):
    marker = tmp_path / "rollback.txt"
    manifest_path = _write_manifest(tmp_path, "rollback")
    good_lifecycle = f'''
from pathlib import Path
MARKER = Path({str(marker)!r})
async def setup(ctx):
    MARKER.write_text("old-setup\\n")
async def dispose(ctx):
    MARKER.write_text(MARKER.read_text() + "old-dispose\\n")
'''
    entrypoint = manifest_path.parent / "tools.py"
    entrypoint.write_text(
        _tool_source("rollback_tool", title="old", lifecycle=good_lifecycle),
        encoding="utf-8",
    )
    await isolated_registry.reconcile_platform_plugins(tmp_path)
    old_generation = isolated_registry._loaded_platform_plugins["rollback"]
    old_tool = isolated_registry._tools["rollback_tool"]

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["version"] = "2.0.0"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    failed_lifecycle = f'''
from pathlib import Path
MARKER = Path({str(marker)!r})
async def setup(ctx):
    MARKER.write_text(MARKER.read_text() + "new-setup-failed\\n")
    raise RuntimeError("setup exploded")
async def dispose(ctx):
    MARKER.write_text(MARKER.read_text() + "failed-stage-dispose\\n")
'''
    entrypoint.write_text(
        _tool_source("rollback_tool", title="new", lifecycle=failed_lifecycle),
        encoding="utf-8",
    )

    assert await isolated_registry.reconcile_platform_plugins(tmp_path) is True
    assert isolated_registry._loaded_platform_plugins["rollback"] is old_generation
    assert isolated_registry._tools["rollback_tool"] is old_tool
    assert (await old_tool.execute({}, None)).title == "old"
    assert marker.read_text(encoding="utf-8").splitlines() == [
        "old-setup",
        "new-setup-failed",
        "failed-stage-dispose",
    ]
    assert all(module_id in sys.modules for module_id in old_generation.module_ids)
    active_ids = {
        module_id
        for module_id in sys.modules
        if module_id.startswith("_openbox_platform_plugin_rollback_")
    }
    assert active_ids == set(old_generation.module_ids)


@pytest.mark.asyncio
async def test_setup_cannot_mutate_live_registry_outside_lifecycle_contract(
    tmp_path: Path,
    isolated_registry,
):
    manifest = _write_manifest(tmp_path, "side-effect")
    lifecycle = '''
async def setup(ctx):
    from tool.registry import register
    register(plugin_tool)
'''
    (manifest.parent / "tools.py").write_text(
        _tool_source("side_effect_tool", title="bad", lifecycle=lifecycle),
        encoding="utf-8",
    )

    assert await isolated_registry.reconcile_platform_plugins(tmp_path) is True
    assert set(isolated_registry._tools) == {"bash"}
    assert isolated_registry._loaded_platform_plugins == {}
    assert not any(
        module_id.startswith("_openbox_platform_plugin_side_effect_")
        for module_id in sys.modules
    )


@pytest.mark.asyncio
async def test_replace_retires_stale_tool_and_drains_in_flight_call(
    tmp_path: Path,
    isolated_registry,
    monkeypatch,
):
    manifest_path = _write_manifest(tmp_path, "drain")
    entrypoint = manifest_path.parent / "tools.py"
    entrypoint.write_text(
        _tool_source(
            "drain_tool",
            title="old",
            execute_body=(
                "ctx.started.set()\n"
                "    await ctx.release.wait()\n"
                '    return ToolResult(title="old", output=args.value)'
            ),
        ),
        encoding="utf-8",
    )
    await isolated_registry.reconcile_platform_plugins(tmp_path)
    old_generation = isolated_registry._loaded_platform_plugins["drain"]
    old_tool = isolated_registry._tools["drain_tool"]

    class Context:
        started = asyncio.Event()
        release = asyncio.Event()

    ctx = Context()
    invocation = asyncio.create_task(old_tool.execute({}, ctx))
    await ctx.started.wait()
    diagnostic_wait = None
    reconcile = None
    diagnostics: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "tool.plugin_lifecycle.log.warning",
        lambda *args, **_kwargs: diagnostics.append(args),
    )
    try:
        diagnostic_wait = asyncio.create_task(
            old_generation.wait_quiescent(diagnostic_interval_seconds=0.05)
        )
        for _ in range(200):
            if diagnostics:
                break
            await asyncio.sleep(0.01)
        assert diagnostic_wait.done() is False
        assert diagnostics
        assert "Waiting for platform plugin quiescence" in str(diagnostics[0][0])

        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["version"] = "2.0.0"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        entrypoint.write_text(_tool_source("drain_tool", title="new"), encoding="utf-8")
        reconcile = asyncio.create_task(isolated_registry.reconcile_platform_plugins(tmp_path))
        for _ in range(100):
            if isolated_registry._tools.get("drain_tool") is not old_tool:
                break
            await asyncio.sleep(0.001)

        new_tool = isolated_registry._tools["drain_tool"]
        assert new_tool is not old_tool
        assert old_generation.in_flight == 1
        assert reconcile.done() is False
        with pytest.raises(PluginGenerationRetired):
            await old_tool.execute({}, ctx)

        ctx.release.set()
        assert (await invocation).title == "old"
        await diagnostic_wait
        assert await reconcile is True
        assert old_generation.disposed is True
        assert not set(old_generation.module_ids).intersection(sys.modules)
        assert (await new_tool.execute({}, None)).title == "new"
    finally:
        # A failed assertion must never strand the full test suite behind the
        # intentionally pinned invocation used by this test.
        ctx.release.set()
        if not invocation.done():
            await invocation
        for task in (diagnostic_wait, reconcile):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


@pytest.mark.asyncio
async def test_dispose_failure_isolated_during_disable_and_remove(
    tmp_path: Path,
    isolated_registry,
):
    marker = tmp_path / "dispose.txt"
    first = _write_manifest(tmp_path, "first")
    second = _write_manifest(tmp_path, "second")
    first_lifecycle = f'''
from pathlib import Path
MARKER = Path({str(marker)!r})
async def dispose(ctx):
    MARKER.write_text((MARKER.read_text() if MARKER.exists() else "") + "first\\n")
    raise RuntimeError("expected disposer failure")
'''
    second_lifecycle = f'''
from pathlib import Path
MARKER = Path({str(marker)!r})
async def dispose(ctx):
    MARKER.write_text((MARKER.read_text() if MARKER.exists() else "") + "second\\n")
'''
    (first.parent / "tools.py").write_text(
        _tool_source("first_tool", title="first", lifecycle=first_lifecycle),
        encoding="utf-8",
    )
    (second.parent / "tools.py").write_text(
        _tool_source("second_tool", title="second", lifecycle=second_lifecycle),
        encoding="utf-8",
    )
    await isolated_registry.reconcile_platform_plugins(tmp_path)
    module_ids = {
        module_id
        for generation in isolated_registry._loaded_platform_plugins.values()
        for module_id in generation.module_ids
    }

    payload = json.loads(first.read_text(encoding="utf-8"))
    payload["enabled"] = False
    first.write_text(json.dumps(payload), encoding="utf-8")
    second.unlink()
    assert await isolated_registry.reconcile_platform_plugins(tmp_path) is True

    assert set(isolated_registry._tools) == {"bash"}
    assert isolated_registry._loaded_platform_plugins == {}
    assert set(marker.read_text(encoding="utf-8").splitlines()) == {"first", "second"}
    assert not module_ids.intersection(sys.modules)


@pytest.mark.asyncio
async def test_effect_stack_is_lifo_and_entrypoint_change_hot_reloads(
    tmp_path: Path,
    isolated_registry,
):
    marker = tmp_path / "effects.txt"
    manifest = _write_manifest(tmp_path, "effects")
    lifecycle = f'''
from pathlib import Path
MARKER = Path({str(marker)!r})
def append(value):
    MARKER.write_text((MARKER.read_text() if MARKER.exists() else "") + value + "\\n")
async def setup(ctx):
    await ctx.effect(lambda: (lambda: append("effect-one")))
    return lambda: append("effect-two")
async def dispose(ctx):
    append("module-dispose")
'''
    (manifest.parent / "tools.py").write_text(
        _tool_source("effect_tool", title="old", lifecycle=lifecycle),
        encoding="utf-8",
    )
    await isolated_registry.reconcile_platform_plugins(tmp_path)
    old_generation = isolated_registry._loaded_platform_plugins["effects"]

    next_path = manifest.parent / "next.py"
    next_path.write_text(_tool_source("effect_tool", title="new"), encoding="utf-8")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["entrypoints"] = ["next.py"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    await isolated_registry.reconcile_platform_plugins(tmp_path)

    assert marker.read_text(encoding="utf-8").splitlines() == [
        "effect-two",
        "effect-one",
        "module-dispose",
    ]
    assert old_generation.disposed is True
    assert (await isolated_registry._tools["effect_tool"].execute({}, None)).title == "new"


@pytest.mark.asyncio
async def test_concurrent_reconcile_serializes_same_plugin_setup(
    tmp_path: Path,
    isolated_registry,
):
    marker = tmp_path / "setups.txt"
    manifest = _write_manifest(tmp_path, "serial")
    lifecycle = f'''
from pathlib import Path
MARKER = Path({str(marker)!r})
async def setup(ctx):
    await __import__("asyncio").sleep(0.02)
    MARKER.write_text((MARKER.read_text() if MARKER.exists() else "") + "setup\\n")
'''
    (manifest.parent / "tools.py").write_text(
        _tool_source("serial_tool", title="serial", lifecycle=lifecycle),
        encoding="utf-8",
    )

    results = await asyncio.gather(
        isolated_registry.reconcile_platform_plugins(tmp_path),
        isolated_registry.reconcile_platform_plugins(tmp_path),
    )
    assert results == [True, True]
    assert marker.read_text(encoding="utf-8").splitlines() == ["setup"]
    assert len(isolated_registry._loaded_platform_plugins) == 1


@pytest.mark.asyncio
async def test_registry_pointer_cas_retries_without_losing_concurrent_registration(
    tmp_path: Path,
    isolated_registry,
):
    marker = tmp_path / "cas.txt"
    manifest = _write_manifest(tmp_path, "cas-plugin")
    lifecycle = f'''
from pathlib import Path
MARKER = Path({str(marker)!r})
def append(value):
    MARKER.write_text((MARKER.read_text() if MARKER.exists() else "") + value + "\\n")
async def setup(ctx):
    append("setup")
    await __import__("asyncio").sleep(0.03)
async def dispose(ctx):
    append("dispose")
'''
    (manifest.parent / "tools.py").write_text(
        _tool_source("cas_tool", title="cas", lifecycle=lifecycle),
        encoding="utf-8",
    )

    reconcile = asyncio.create_task(
        isolated_registry.reconcile_platform_plugins(tmp_path)
    )
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(0.001)
    assert marker.exists()
    concurrent_tool = SimpleNamespace(id="concurrent_builtin")
    isolated_registry.register(concurrent_tool)

    assert await reconcile is True
    assert isolated_registry._tools["concurrent_builtin"] is concurrent_tool
    assert "cas_tool" in isolated_registry._tools
    # The first invisible generation lost CAS and was disposed; the retry is
    # the only published generation.
    assert marker.read_text(encoding="utf-8").splitlines() == [
        "setup",
        "dispose",
        "setup",
    ]


@pytest.mark.asyncio
async def test_watcher_converges_source_disable_enable_and_remove(
    tmp_path: Path,
    isolated_registry,
):
    manifest = _write_manifest(tmp_path, "watched")
    entrypoint = manifest.parent / "tools.py"
    entrypoint.write_text(
        _tool_source("watched_tool", title="old"),
        encoding="utf-8",
    )
    watcher = isolated_registry.PlatformPluginWatcher()
    await watcher.start(workspace_root=tmp_path, interval_seconds=0.25)
    try:
        old_tool = await _eventually(
            lambda: isolated_registry._tools.get("watched_tool")
        )
        old_generation = isolated_registry._loaded_platform_plugins["watched"]

        # Source-only same-path edits are real hot replacement; they do not
        # rely on a version bump or timestamp-based bytecode cache.
        entrypoint.write_text(
            _tool_source("watched_tool", title="new"),
            encoding="utf-8",
        )
        await _eventually(
            lambda: (
                isolated_registry._loaded_platform_plugins.get("watched")
                is not old_generation
            )
        )
        new_tool = isolated_registry._tools["watched_tool"]
        assert new_tool is not old_tool
        assert (await new_tool.execute({}, None)).title == "new"

        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["enabled"] = False
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        await _eventually(
            lambda: "watched_tool" not in isolated_registry._tools
        )

        payload["enabled"] = True
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        await _eventually(
            lambda: isolated_registry._tools.get("watched_tool")
        )
        manifest.unlink()
        await _eventually(
            lambda: "watched_tool" not in isolated_registry._tools
        )
    finally:
        await watcher.stop()
    assert watcher.running is False


@pytest.mark.asyncio
async def test_watcher_survives_unexpected_reconcile_exception(
    tmp_path: Path,
    isolated_registry,
    monkeypatch,
):
    attempts = 0
    recovered = asyncio.Event()

    async def flaky_reconcile(_workspace_root):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient watcher defect")
        recovered.set()
        return True

    monkeypatch.setattr(
        isolated_registry,
        "reconcile_platform_plugins",
        flaky_reconcile,
    )
    watcher = isolated_registry.PlatformPluginWatcher()
    await watcher.start(workspace_root=tmp_path, interval_seconds=0.25)
    try:
        await asyncio.wait_for(recovered.wait(), timeout=2.0)
        assert watcher.running is True
        assert attempts >= 2
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_sync_reconcile_refuses_to_deadlock_an_active_watcher(
    tmp_path: Path,
    isolated_registry,
    monkeypatch,
):
    watcher = isolated_registry.platform_plugin_watcher
    await watcher.start(workspace_root=tmp_path, interval_seconds=3600)
    monkeypatch.chdir(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="await reconcile_platform_plugins"):
            isolated_registry.register_custom_tools()
    finally:
        await watcher.stop()
