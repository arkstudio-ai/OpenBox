import json
import sys
from pathlib import Path

import pytest

from tool.platform_plugins import (
    PlatformPluginError,
    discover_plugin_manifests,
    load_platform_plugin,
    read_plugin_manifest,
)


TOOL_SOURCE = '''
from pydantic import BaseModel
from tool.tool import ToolInfo, ToolResult

class Input(BaseModel):
    value: str = "ok"

async def run(args: Input, ctx):
    return ToolResult(title="plugin", output=args.value)

plugin_tool = ToolInfo(
    id="plugin_echo",
    description="Echo through a trusted platform plugin",
    parameters=Input,
    execute=run,
    source="builtin",
    plane="sandbox",
    canonical_id="forged",
    provider_name="forged",
    pack="video",
    same_response_safe=True,
    parallel_safe=True,
)
'''


def write_manifest(
    root: Path,
    *,
    name: str = "sample",
    enabled: bool = True,
    entrypoints: list[str] | None = None,
) -> Path:
    directory = root / ".openbox" / "plugins" / name
    directory.mkdir(parents=True)
    manifest = directory / "plugin.json"
    manifest.write_text(
        json.dumps({
            "schema_version": 1,
            "name": name,
            "version": "1.2.3",
            "enabled": enabled,
            "entrypoints": entrypoints or ["tools.py"],
        }),
        encoding="utf-8",
    )
    return manifest


@pytest.mark.asyncio
async def test_manifest_plugin_loads_atomically_and_registration_owns_trust(tmp_path: Path):
    manifest_path = write_manifest(tmp_path)
    (manifest_path.parent / "tools.py").write_text(TOOL_SOURCE, encoding="utf-8")

    manifest = read_plugin_manifest(manifest_path)
    tools = load_platform_plugin(manifest, reserved_ids={"bash"})

    assert set(tools) == {"plugin_echo"}
    tool = tools["plugin_echo"]
    assert tool.source == "custom"
    assert tool.plane == "platform"
    assert tool.canonical_id == "plugin_echo"
    assert tool.provider_name == "plugin_echo"
    assert tool.pack is None
    assert tool.same_response_safe is False
    assert tool.parallel_safe is False
    result = await tool.execute({"value": "hello"}, None)
    assert result.output == "hello"


def test_builtin_collision_rejects_the_complete_plugin_and_cleans_modules(tmp_path: Path):
    manifest_path = write_manifest(tmp_path)
    source = TOOL_SOURCE + '\nbuiltin_collision = ToolInfo(\n    id="bash",\n    description="bad",\n    parameters=Input,\n    execute=run,\n)\n'
    (manifest_path.parent / "tools.py").write_text(source, encoding="utf-8")
    before = set(sys.modules)

    with pytest.raises(PlatformPluginError, match="collides"):
        load_platform_plugin(read_plugin_manifest(manifest_path), reserved_ids={"bash"})

    leaked = set(sys.modules) - before
    assert not any(name.startswith("_openbox_platform_plugin_sample_") for name in leaked)


def test_disabled_plugin_is_not_imported(tmp_path: Path):
    manifest_path = write_manifest(tmp_path, enabled=False)
    (manifest_path.parent / "tools.py").write_text("raise RuntimeError('must not import')", encoding="utf-8")

    assert load_platform_plugin(read_plugin_manifest(manifest_path), reserved_ids=set()) == {}


def test_entrypoint_cannot_escape_or_use_a_symlink(tmp_path: Path):
    manifest_path = write_manifest(tmp_path, entrypoints=["../outside.py"])
    (manifest_path.parent.parent / "outside.py").write_text(TOOL_SOURCE, encoding="utf-8")

    with pytest.raises(PlatformPluginError, match="direct|escapes"):
        load_platform_plugin(read_plugin_manifest(manifest_path), reserved_ids=set())

    manifest_path = write_manifest(tmp_path, name="linked")
    target = tmp_path / "target.py"
    target.write_text(TOOL_SOURCE, encoding="utf-8")
    (manifest_path.parent / "tools.py").symlink_to(target)
    with pytest.raises(PlatformPluginError, match="regular Python file"):
        load_platform_plugin(read_plugin_manifest(manifest_path), reserved_ids=set())


def test_legacy_tools_are_discovered_as_one_transactional_plugin(tmp_path: Path):
    directory = tmp_path / ".openbox" / "tools"
    directory.mkdir(parents=True)
    (directory / "echo.py").write_text(TOOL_SOURCE, encoding="utf-8")

    manifests = discover_plugin_manifests(tmp_path)

    assert len(manifests) == 1
    assert manifests[0].legacy is True
    assert manifests[0].entrypoints == ("echo.py",)
    assert set(load_platform_plugin(manifests[0], reserved_ids=set())) == {"plugin_echo"}


def test_legacy_tools_apply_the_same_entrypoint_bound(tmp_path: Path):
    directory = tmp_path / ".openbox" / "tools"
    directory.mkdir(parents=True)
    for index in range(33):
        (directory / f"tool_{index:02d}.py").write_text(TOOL_SOURCE, encoding="utf-8")

    with pytest.raises(PlatformPluginError, match="too many entrypoints"):
        discover_plugin_manifests(tmp_path)


def test_manifest_identity_is_versioned_and_matches_directory(tmp_path: Path):
    manifest_path = write_manifest(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PlatformPluginError, match="unsupported"):
        read_plugin_manifest(manifest_path)

    payload["schema_version"] = 1
    payload["name"] = "different"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PlatformPluginError, match="match its directory"):
        read_plugin_manifest(manifest_path)

    payload["name"] = "sample"
    payload["unexpected"] = True
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PlatformPluginError, match="unknown fields"):
        read_plugin_manifest(manifest_path)


def test_plugin_directory_cannot_be_a_symlink(tmp_path: Path):
    outside = tmp_path / "outside" / "sample"
    outside.mkdir(parents=True)
    (outside / "plugin.json").write_text(
        json.dumps({
            "schema_version": 1,
            "name": "sample",
            "version": "1.0.0",
            "entrypoints": ["tools.py"],
        }),
        encoding="utf-8",
    )
    (outside / "tools.py").write_text(TOOL_SOURCE, encoding="utf-8")
    plugin_root = tmp_path / ".openbox" / "plugins"
    plugin_root.mkdir(parents=True)
    (plugin_root / "sample").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PlatformPluginError, match="escapes|symlink"):
        discover_plugin_manifests(tmp_path)


def test_openbox_ancestor_cannot_be_a_symlink(tmp_path: Path):
    outside = tmp_path / "outside"
    plugin = outside / "plugins" / "sample"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(
        json.dumps({
            "schema_version": 1,
            "name": "sample",
            "version": "1.0.0",
            "entrypoints": ["tools.py"],
        }),
        encoding="utf-8",
    )
    (plugin / "tools.py").write_text(TOOL_SOURCE, encoding="utf-8")
    (tmp_path / ".openbox").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PlatformPluginError, match="symlink"):
        discover_plugin_manifests(tmp_path)


def test_workspace_root_itself_cannot_be_a_symlink(tmp_path: Path):
    real_root = tmp_path / "real"
    manifest_path = write_manifest(real_root)
    (manifest_path.parent / "tools.py").write_text(TOOL_SOURCE, encoding="utf-8")
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(PlatformPluginError, match="symlink"):
        discover_plugin_manifests(linked_root)


def test_workspace_parent_ancestor_cannot_be_a_symlink(tmp_path: Path):
    real_parent = tmp_path / "real-parent"
    real_root = real_parent / "workspace"
    manifest_path = write_manifest(real_root)
    (manifest_path.parent / "tools.py").write_text(TOOL_SOURCE, encoding="utf-8")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(PlatformPluginError, match="symlink"):
        discover_plugin_manifests(linked_parent / "workspace")


def test_loader_rechecks_ancestor_symlinks_after_discovery(tmp_path: Path):
    manifest_path = write_manifest(tmp_path)
    (manifest_path.parent / "tools.py").write_text(TOOL_SOURCE, encoding="utf-8")
    manifest = discover_plugin_manifests(tmp_path)[0]

    openbox = tmp_path / ".openbox"
    moved = tmp_path / "moved-openbox"
    openbox.rename(moved)
    openbox.symlink_to(moved, target_is_directory=True)

    with pytest.raises(PlatformPluginError, match="symlink"):
        load_platform_plugin(manifest, reserved_ids=set())


def test_legacy_plugin_name_is_reserved(tmp_path: Path):
    manifest_path = write_manifest(tmp_path, name="legacy-tools")
    with pytest.raises(PlatformPluginError, match="reserved"):
        read_plugin_manifest(manifest_path)


def test_registry_rejects_plugin_side_effect_registration(tmp_path: Path, monkeypatch):
    from tool import registry

    manifest_path = write_manifest(tmp_path)
    (manifest_path.parent / "tools.py").write_text(
        TOOL_SOURCE + "\nfrom tool.registry import register\nregister(plugin_tool)\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    original = registry._tools
    monkeypatch.setattr(registry, "_tools", {"bash": object()})
    try:
        registry.register_custom_tools()
        assert set(registry._tools) == {"bash"}
    finally:
        monkeypatch.setattr(registry, "_tools", original)


def test_direct_loader_rejects_plugin_side_effect_registration(tmp_path: Path, monkeypatch):
    from tool import registry

    manifest_path = write_manifest(tmp_path, name="direct-side-effect")
    (manifest_path.parent / "tools.py").write_text(
        TOOL_SOURCE + "\nfrom tool.registry import register\nregister(plugin_tool)\n",
        encoding="utf-8",
    )
    original = registry._tools
    monkeypatch.setattr(registry, "_tools", {"bash": object()})
    try:
        with pytest.raises(RuntimeError, match="must export ToolInfo"):
            load_platform_plugin(read_plugin_manifest(manifest_path), reserved_ids={"bash"})
        assert set(registry._tools) == {"bash"}
    finally:
        monkeypatch.setattr(registry, "_tools", original)


def test_registry_unloads_plugin_when_manifest_is_disabled(tmp_path: Path, monkeypatch):
    from tool import registry

    manifest_path = write_manifest(tmp_path, name="disable-me")
    (manifest_path.parent / "tools.py").write_text(TOOL_SOURCE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    original_tools = registry._tools
    original_loaded = registry._loaded_platform_plugins
    monkeypatch.setattr(registry, "_tools", {"bash": object()})
    monkeypatch.setattr(registry, "_loaded_platform_plugins", {})
    try:
        registry.register_custom_tools()
        assert "plugin_echo" in registry._tools

        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["enabled"] = False
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        registry.register_custom_tools()

        assert set(registry._tools) == {"bash"}
        assert registry._loaded_platform_plugins == {}
    finally:
        monkeypatch.setattr(registry, "_tools", original_tools)
        monkeypatch.setattr(registry, "_loaded_platform_plugins", original_loaded)


def test_registry_repeated_startup_is_idempotent(tmp_path: Path, monkeypatch):
    from tool import registry

    manifest_path = write_manifest(tmp_path, name="start-once")
    source = (
        "from pathlib import Path\n"
        "marker = Path(__file__).with_name('imports.txt')\n"
        "marker.write_text(marker.read_text() + 'x' if marker.exists() else 'x')\n"
        + TOOL_SOURCE
    )
    (manifest_path.parent / "tools.py").write_text(source, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(registry, "_tools", {"bash": object()})
    monkeypatch.setattr(registry, "_loaded_platform_plugins", {})

    registry.register_custom_tools()
    first = registry._tools["plugin_echo"]
    registry.register_custom_tools()

    assert registry._tools["plugin_echo"] is first
    assert set(registry._tools) == {"bash", "plugin_echo"}
    assert (manifest_path.parent / "imports.txt").read_text(encoding="utf-8") == "x"


def test_registry_unloads_plugin_when_manifests_disappear(tmp_path: Path, monkeypatch):
    from tool import registry

    manifest_path = write_manifest(tmp_path, name="remove-me")
    (manifest_path.parent / "tools.py").write_text(TOOL_SOURCE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(registry, "_tools", {"bash": object()})
    monkeypatch.setattr(registry, "_loaded_platform_plugins", {})

    registry.register_custom_tools()
    assert "plugin_echo" in registry._tools
    manifest_path.unlink()
    registry.register_custom_tools()

    assert set(registry._tools) == {"bash"}
    assert registry._loaded_platform_plugins == {}


def test_legacy_entrypoint_set_reconciles_in_the_same_process(tmp_path: Path, monkeypatch):
    from tool import registry

    legacy_dir = tmp_path / ".openbox" / "tools"
    legacy_dir.mkdir(parents=True)
    first_path = legacy_dir / "first.py"
    first_path.write_text(TOOL_SOURCE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(registry, "_tools", {"bash": object()})
    monkeypatch.setattr(registry, "_loaded_platform_plugins", {})

    registry.register_custom_tools()
    assert set(registry._tools) == {"bash", "plugin_echo"}

    (legacy_dir / "second.py").write_text(
        TOOL_SOURCE.replace('id="plugin_echo"', 'id="plugin_second"'),
        encoding="utf-8",
    )
    registry.register_custom_tools()
    assert set(registry._tools) == {"bash", "plugin_echo", "plugin_second"}

    first_path.unlink()
    registry.register_custom_tools()
    assert set(registry._tools) == {"bash", "plugin_second"}


def test_failed_legacy_reload_keeps_the_previous_generation(tmp_path: Path, monkeypatch):
    from tool import registry

    legacy_dir = tmp_path / ".openbox" / "tools"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "good.py").write_text(TOOL_SOURCE, encoding="utf-8")
    builtin = object()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(registry, "_tools", {"bash": builtin})
    monkeypatch.setattr(registry, "_loaded_platform_plugins", {})

    registry.register_custom_tools()
    previous = registry._tools["plugin_echo"]
    (legacy_dir / "bad.py").write_text(
        TOOL_SOURCE.replace('id="plugin_echo"', 'id="bash"'),
        encoding="utf-8",
    )
    registry.register_custom_tools()

    assert registry._tools["bash"] is builtin
    assert registry._tools["plugin_echo"] is previous
    assert set(registry._loaded_platform_plugins) == {"legacy-tools"}


def test_disabled_modern_plugin_releases_its_tool_id_to_legacy(tmp_path: Path, monkeypatch):
    from tool import registry

    manifest_path = write_manifest(tmp_path, name="modern")
    (manifest_path.parent / "tools.py").write_text(
        TOOL_SOURCE.replace('title="plugin"', 'title="modern"'),
        encoding="utf-8",
    )
    legacy_dir = tmp_path / ".openbox" / "tools"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "echo.py").write_text(
        TOOL_SOURCE.replace('title="plugin"', 'title="legacy"'),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(registry, "_tools", {"bash": object()})
    monkeypatch.setattr(registry, "_loaded_platform_plugins", {})

    registry.register_custom_tools()
    assert set(registry._loaded_platform_plugins) == {"modern"}

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["enabled"] = False
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    registry.register_custom_tools()

    assert set(registry._loaded_platform_plugins) == {"legacy-tools"}
    assert registry._tools["plugin_echo"].source == "custom"


@pytest.mark.asyncio
async def test_modern_plugin_wins_duplicate_tool_added_after_legacy_startup(
    tmp_path: Path,
    monkeypatch,
):
    from tool import registry

    legacy_dir = tmp_path / ".openbox" / "tools"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "echo.py").write_text(
        TOOL_SOURCE.replace('title="plugin"', 'title="legacy"'),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(registry, "_tools", {"bash": object()})
    monkeypatch.setattr(registry, "_loaded_platform_plugins", {})

    registry.register_custom_tools()
    legacy_tool = registry._tools["plugin_echo"]
    assert (await legacy_tool.execute({}, None)).title == "legacy"

    manifest_path = write_manifest(tmp_path, name="modern")
    (manifest_path.parent / "tools.py").write_text(
        TOOL_SOURCE.replace('title="plugin"', 'title="modern"'),
        encoding="utf-8",
    )
    registry.register_custom_tools()

    modern_tool = registry._tools["plugin_echo"]
    assert modern_tool is not legacy_tool
    assert (await modern_tool.execute({}, None)).title == "modern"
    assert set(registry._loaded_platform_plugins) == {"modern"}


def test_unload_does_not_remove_an_untracked_replacement(tmp_path: Path, monkeypatch):
    from dataclasses import replace

    from tool import registry

    manifest_path = write_manifest(tmp_path, name="owned")
    (manifest_path.parent / "tools.py").write_text(TOOL_SOURCE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(registry, "_tools", {"bash": object()})
    monkeypatch.setattr(registry, "_loaded_platform_plugins", {})

    registry.register_custom_tools()
    replacement = replace(registry._tools["plugin_echo"])
    registry._tools["plugin_echo"] = replacement
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["enabled"] = False
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    registry.register_custom_tools()

    assert registry._tools["plugin_echo"] is replacement
    assert registry._loaded_platform_plugins == {}


def test_collision_does_not_mutate_an_imported_builtin_tool(tmp_path: Path):
    from tool.read import read_tool

    manifest_path = write_manifest(tmp_path)
    (manifest_path.parent / "tools.py").write_text(
        "from tool.read import read_tool\n",
        encoding="utf-8",
    )
    before = (
        read_tool.source,
        read_tool.plane,
        read_tool.canonical_id,
        read_tool.provider_name,
        read_tool.parallel_safe,
    )

    with pytest.raises(PlatformPluginError, match="collides"):
        load_platform_plugin(read_plugin_manifest(manifest_path), reserved_ids={"read"})

    assert (
        read_tool.source,
        read_tool.plane,
        read_tool.canonical_id,
        read_tool.provider_name,
        read_tool.parallel_safe,
    ) == before


def test_reloading_the_same_path_reuses_module_without_reexecuting(tmp_path: Path):
    manifest_path = write_manifest(tmp_path)
    source = (
        "from pathlib import Path\n"
        "marker = Path(__file__).with_name('imports.txt')\n"
        "marker.write_text(marker.read_text() + 'x' if marker.exists() else 'x')\n"
        + TOOL_SOURCE
    )
    (manifest_path.parent / "tools.py").write_text(source, encoding="utf-8")
    manifest = read_plugin_manifest(manifest_path)

    load_platform_plugin(manifest, reserved_ids=set())
    load_platform_plugin(manifest, reserved_ids=set())

    assert (manifest_path.parent / "imports.txt").read_text(encoding="utf-8") == "x"


@pytest.mark.asyncio
async def test_raw_plugin_arguments_are_validated_into_the_declared_model(tmp_path: Path):
    manifest_path = write_manifest(tmp_path, name="typed-args")
    source = TOOL_SOURCE.replace('value: str = "ok"', "value: str")
    (manifest_path.parent / "tools.py").write_text(source, encoding="utf-8")
    tool = load_platform_plugin(read_plugin_manifest(manifest_path), reserved_ids=set())["plugin_echo"]

    valid = await tool.execute({"value": "typed"}, None)
    invalid = await tool.execute({}, None)

    assert valid.output == "typed"
    assert invalid.title == "Invalid input for plugin_echo"
    assert "validation error" in invalid.output.lower()


@pytest.mark.asyncio
async def test_legacy_raw_dict_executor_remains_compatible(tmp_path: Path):
    manifest_path = write_manifest(tmp_path, name="legacy-dict")
    source = TOOL_SOURCE.replace(
        "async def run(args: Input, ctx):\n    return ToolResult(title=\"plugin\", output=args.value)",
        "async def run(args, ctx):\n    return ToolResult(title=\"plugin\", output=args[\"value\"])",
    )
    (manifest_path.parent / "tools.py").write_text(source, encoding="utf-8")
    tool = load_platform_plugin(read_plugin_manifest(manifest_path), reserved_ids=set())["plugin_echo"]

    result = await tool.execute({"value": "legacy"}, None)

    assert result.output == "legacy"


@pytest.mark.asyncio
async def test_unannotated_model_style_executor_uses_validated_arguments(tmp_path: Path):
    manifest_path = write_manifest(tmp_path, name="legacy-attribute")
    source = TOOL_SOURCE.replace("async def run(args: Input, ctx):", "async def run(args, ctx):")
    (manifest_path.parent / "tools.py").write_text(source, encoding="utf-8")
    tool = load_platform_plugin(read_plugin_manifest(manifest_path), reserved_ids=set())["plugin_echo"]

    result = await tool.execute({"value": "attribute"}, None)

    assert result.output == "attribute"


@pytest.mark.asyncio
async def test_define_tool_plugin_uses_the_same_runtime_call_contract(tmp_path: Path):
    manifest_path = write_manifest(tmp_path, name="factory-tool")
    (manifest_path.parent / "tools.py").write_text(
        '''
from pydantic import BaseModel
from tool.tool import ToolResult, define_tool

class Input(BaseModel):
    value: str

async def run(args: Input, ctx):
    return ToolResult(title="factory", output=args.value)

plugin_tool = define_tool(
    "plugin_echo",
    description="factory plugin",
    parameters=Input,
    execute=run,
)
''',
        encoding="utf-8",
    )
    tool = load_platform_plugin(read_plugin_manifest(manifest_path), reserved_ids=set())["plugin_echo"]

    result = await tool.execute({"value": "works"}, None)

    assert result.title == "factory"
    assert result.output == "works"


def test_plugin_contract_rejects_a_mismatched_typed_argument_model(tmp_path: Path):
    manifest_path = write_manifest(tmp_path, name="wrong-model")
    source = TOOL_SOURCE.replace(
        "class Input(BaseModel):\n    value: str = \"ok\"",
        "class Input(BaseModel):\n    value: str = \"ok\"\n\nclass OtherInput(BaseModel):\n    value: str",
    ).replace("async def run(args: Input, ctx):", "async def run(args: OtherInput, ctx):")
    (manifest_path.parent / "tools.py").write_text(source, encoding="utf-8")

    with pytest.raises(PlatformPluginError, match="contract"):
        load_platform_plugin(read_plugin_manifest(manifest_path), reserved_ids=set())


@pytest.mark.asyncio
async def test_plugin_runtime_enforces_tool_result_contract(tmp_path: Path):
    manifest_path = write_manifest(tmp_path, name="wrong-result")
    source = TOOL_SOURCE.replace(
        'return ToolResult(title="plugin", output=args.value)',
        'return {"title": "plugin", "output": args.value}',
    )
    (manifest_path.parent / "tools.py").write_text(source, encoding="utf-8")
    tool = load_platform_plugin(read_plugin_manifest(manifest_path), reserved_ids=set())["plugin_echo"]

    with pytest.raises(TypeError, match="ToolResult"):
        await tool.execute({"value": "bad"}, None)


def test_module_identity_includes_manifest_version(tmp_path: Path):
    manifest_path = write_manifest(tmp_path, name="versioned-module")
    source = (
        "from pathlib import Path\n"
        "marker = Path(__file__).with_name('imports.txt')\n"
        "marker.write_text(marker.read_text() + 'x' if marker.exists() else 'x')\n"
        + TOOL_SOURCE
    )
    (manifest_path.parent / "tools.py").write_text(source, encoding="utf-8")

    load_platform_plugin(read_plugin_manifest(manifest_path), reserved_ids=set())
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["version"] = "2.0.0"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    load_platform_plugin(read_plugin_manifest(manifest_path), reserved_ids=set())

    assert (manifest_path.parent / "imports.txt").read_text(encoding="utf-8") == "xx"
