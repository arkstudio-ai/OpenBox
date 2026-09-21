from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_catalog.compiler import compile_agent
from skill import snapshot as snapshots
from skill.provider import ScopeKey, SkillCatalogSnapshot, SkillDefinition, SkillScopeMismatch
from team.errors import TeamError
from team.journal import Actor
from tests.unit.test_subagent_composition import _config
from tests.unit.test_team_compiler import spec
from trajectory.storage import MemoryBlobStore


class Registry:
    def __init__(self, *skills):
        self.skills = {skill.name: skill for skill in skills}

    async def snapshot(self, scope):
        return SkillCatalogSnapshot(scope, tuple(self.skills.values()), True, "live", ())

    async def load(self, snapshot, name, *, scope):
        return self.skills.get(name)


@pytest.fixture
def store(monkeypatch):
    store = MemoryBlobStore()
    monkeypatch.setattr(snapshots, "get_blob_store", lambda: store)
    return store


async def test_selected_skill_body_frozen_but_revocation_is_live(store):
    actor = Actor("owner", "workspace")
    original = SkillDefinition("one", "Description", "user", "Original body", allowed_tools=("bash",))
    live = Registry(original, replace(original, name="two"))
    definition = spec(skill_refs=[{"name": "one"}])
    entries = await snapshots.freeze_specs([definition], actor, registry=live)
    compiled = compile_agent(definition, config=_config("openai/test"), skills=entries)
    assert "bash" not in compiled.authority.tool_ids
    frozen = snapshots.FrozenSkillRegistry(actor, compiled.summary["skills"], live)
    scope = ScopeKey(user_id="owner")
    catalog = await frozen.snapshot(scope)
    assert [skill.name for skill in catalog.skills] == ["one"]
    live.skills["one"] = replace(original, content="Edited after admission")
    assert (await frozen.load(catalog, "one", scope=scope)).content == "Original body"
    assert await frozen.load(catalog, "two", scope=scope) is None
    del live.skills["one"]
    assert await frozen.load(catalog, "one", scope=scope) is None


async def test_all_accessible_and_blob_scope_fences(store):
    actor = Actor("owner", "workspace")
    live = Registry(SkillDefinition("one", "Desc", "user", "Body"), SkillDefinition("two", "Desc", "user", "Second"))
    definition = spec(skill_mode="all_accessible", skill_refs=[{"name": "one"}])
    entries = await snapshots.freeze_specs([definition], actor, registry=live)
    compiled = compile_agent(definition, config=_config("openai/test"), skills=entries)
    assert len(compiled.summary["skills"]) == 1
    with pytest.raises(TeamError) as error:
        await snapshots.read_content(Actor("another", "workspace"), entries[0])
    assert error.value.status == 403
    frozen = snapshots.FrozenSkillRegistry(actor, entries, live, instance_id="member-first", all_accessible=True)
    with pytest.raises(SkillScopeMismatch):
        await frozen.snapshot(ScopeKey(user_id="another"))
    scope = ScopeKey(user_id="owner")
    catalog = await frozen.snapshot(scope)
    assert len(catalog.skills) == 2
    live.skills["two"] = replace(live.skills["two"], content="Edited before first read")
    assert (await frozen.load(catalog, "two")).content == "Edited before first read"
    live.skills["two"] = replace(live.skills["two"], content="Edited after first read")
    reconstructed = snapshots.FrozenSkillRegistry(actor, entries, live, instance_id="member-first", all_accessible=True)
    assert (await reconstructed.load(await reconstructed.snapshot(scope), "two")).content == "Edited before first read"
    live.skills["three"] = SkillDefinition("three", "Added after admission", "user", "New Skill")
    catalog = await reconstructed.snapshot(scope)
    assert (await reconstructed.load(catalog, "three")).content == "New Skill"
    next_run = snapshots.FrozenSkillRegistry(actor, entries, live, instance_id="member-next", all_accessible=True)
    assert (await next_run.load(await next_run.snapshot(scope), "two")).content == "Edited after first read"


async def test_resource_first_read_is_immutable_and_cannot_escape(store, tmp_path):
    (tmp_path / "reference.txt").write_text("First resource")
    actor = Actor("owner", "workspace")
    live = Registry(SkillDefinition("one", "Desc", "user", "Body", path=str(tmp_path)))
    entries = await snapshots.freeze_specs([spec(skill_refs=[{"name": "one"}])], actor, registry=live)
    frozen = snapshots.FrozenSkillRegistry(actor, entries, live, instance_id="member-first")
    catalog = await frozen.snapshot(ScopeKey(user_id="owner"))
    ctx = SimpleNamespace(sandbox=None, user_id="owner", workspace_id="workspace")
    assert await frozen.resource(catalog, "one", "reference.txt", ctx) == "First resource"
    (tmp_path / "reference.txt").write_text("Changed resource")
    next_run = snapshots.FrozenSkillRegistry(actor, entries, live, instance_id="member-next")
    assert await next_run.resource(await next_run.snapshot(ScopeKey(user_id="owner")), "one", "reference.txt", ctx) == "Changed resource"
    assert await frozen.resource(catalog, "one", "reference.txt", ctx) == "First resource"
    with pytest.raises(TeamError):
        await frozen.resource(catalog, "one", "../reference.txt", ctx)
    with pytest.raises(TeamError):
        await frozen.resource(catalog, "one", "not-listed.txt", ctx)


async def test_missing_selected_skill_and_corrupt_blob_fail_closed(store):
    actor = Actor("owner", "workspace")
    with pytest.raises(TeamError):
        await snapshots.freeze_specs([spec(skill_refs=[{"name": "missing"}])], actor, registry=Registry())
    live = Registry(SkillDefinition("one", "Desc", "user", "Body"))
    entries = await snapshots.freeze_specs([spec(skill_refs=[{"name": "one"}])], actor, registry=live)
    await store.put(entries[0]["blob_key"], b"corrupt", content_type="text/plain", if_absent=False)
    with pytest.raises(TeamError, match="content check"):
        await snapshots.read_content(actor, entries[0])


async def test_project_scope_and_live_coordinator_skill_policy(store, monkeypatch):
    from unittest.mock import AsyncMock
    from team import runtime_binding
    actor = Actor("owner", "workspace")
    scope = ScopeKey(user_id="owner", project_id="project", workdir="/workspace/project")
    live = Registry(SkillDefinition("one", "Desc", "project", "Body"), SkillDefinition("two", "Desc", "user", "Other"))
    entries = await snapshots.freeze_specs([spec(skill_refs=[{"name": "one"}])], actor, registry=live, scope=scope)
    assert entries[0]["scope"]["project_id"] == "project"
    frozen = snapshots.FrozenSkillRegistry(actor, entries, live)
    assert len((await frozen.snapshot(scope)).skills) == 1
    assert not (await frozen.snapshot(ScopeKey(user_id="owner", project_id="other"))).skills
    grant = {"allowed_skills": [{"name": "one"}]}
    monkeypatch.setattr(runtime_binding, "current_grant", AsyncMock(side_effect=lambda: grant))
    coordinator = snapshots.PolicySkillRegistry(live)
    catalog = await coordinator.snapshot(scope)
    assert [item.name for item in catalog.skills] == ["one"]
    assert await coordinator.load(catalog, "two") is None
    grant["allowed_skills"] = []
    assert await coordinator.load(catalog, "one") is None


@pytest.mark.parametrize("sandbox_provider", [False, True])
async def test_binary_resources_and_symlinks_are_checked_in_their_execution_plane(store, tmp_path, sandbox_provider):
    import asyncio
    import base64
    import json
    class LocalSandbox:
        async def execute(self, command, timeout):
            process = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
            return SimpleNamespace(exit_code=process.returncode, stdout=stdout.decode())
    root = tmp_path / "skill"
    root.mkdir()
    (root / "image.bin").write_bytes(b"\x00\xff\x80binary")
    (tmp_path / "outside").write_text("Outside the Skill")
    (root / "escape.txt").symlink_to(tmp_path / "outside")
    location = {"base_dir": str(root), "files": ("image.bin", "escape.txt")} if sandbox_provider else {"path": str(root)}
    live = Registry(SkillDefinition("one", "Desc", "user", "Frozen instructions", **location))
    actor = Actor("owner", "workspace")
    entries = await snapshots.freeze_specs([spec(skill_refs=[{"name": "one"}])], actor, registry=live)
    frozen = snapshots.FrozenSkillRegistry(actor, entries, live)
    catalog = await frozen.snapshot(ScopeKey(user_id="owner"))
    ctx = SimpleNamespace(sandbox=LocalSandbox() if sandbox_provider else None, user_id="owner", workspace_id="workspace")
    first = json.loads(await frozen.resource(catalog, "one", "image.bin", ctx))
    assert first["encoding"] == "base64" and base64.b64decode(first["body"]) == b"\x00\xff\x80binary"
    (root / "image.bin").write_bytes(b"changed")
    assert json.loads(await frozen.resource(catalog, "one", "./image.bin", ctx)) == first
    assert await frozen.resource(catalog, "one", "SKILL.md", ctx) == "Frozen instructions"
    with pytest.raises(TeamError, match="directory"):
        await frozen.resource(catalog, "one", "escape.txt", ctx)


async def test_blank_resource_loads_body_and_storage_failure_is_typed(store, monkeypatch):
    from tool.skill_tool import SkillArgs, _execute_selected_skill
    actor = Actor("owner", "workspace")
    live = Registry(SkillDefinition("one", "Desc", "user", "Body"))
    entries = await snapshots.freeze_specs([spec(skill_refs=[{"name": "one"}])], actor, registry=live)
    frozen = snapshots.FrozenSkillRegistry(actor, entries, live)
    scope = ScopeKey(user_id="owner")
    catalog = await frozen.snapshot(scope)
    ctx = SimpleNamespace(user_id="owner", workspace_id="workspace", project_id="", workdir="")
    from unittest.mock import AsyncMock
    monkeypatch.setattr("tool.skill_tool._record_skill_loaded", AsyncMock())
    result = await _execute_selected_skill(SkillArgs(skill="one", resource=""), ctx, frozen, catalog)
    assert result.title == "Loaded skill: one" and "Body" in result.output
    monkeypatch.setattr(store, "get", AsyncMock(side_effect=OSError("unavailable")))
    with pytest.raises(TeamError) as error:
        await frozen.load(catalog, "one")
    assert error.value.code == "SKILL_SNAPSHOT_UNAVAILABLE"


def test_resource_symlink_swap_after_resolution_cannot_read_outside(tmp_path, monkeypatch):
    import os
    from skill.snapshot_resource import confined_read
    root = tmp_path / "skill"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "reference.txt").write_text("Allowed")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "reference.txt").write_text("Private outside content")
    real_open = os.open
    switched = False
    def swap(path, flags, *args, **kwargs):
        nonlocal switched
        if path == "nested" and kwargs.get("dir_fd") is not None and not switched:
            switched = True
            nested.rename(root / "original")
            nested.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(OSError):
        confined_read(str(root), "nested/reference.txt", 1024)
    assert switched
