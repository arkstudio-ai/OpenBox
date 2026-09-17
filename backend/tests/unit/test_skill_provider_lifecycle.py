"""Scoped Skill provider, cache, merge and load lifecycle contracts."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from project.workspace import user_scope_for_identity
from agent.tool_resolution import attach_skill_listing
from skill.provider import (
    HostFilesystemSkillProvider,
    PersonalLibrarySkillProvider,
    SandboxCatalogueSkillProvider,
    ScopeKey,
    SkillCandidate,
    SkillCatalogSnapshot,
    SkillDefinition,
    SkillProviderSnapshot,
    SkillRegistry,
    SkillScopeMismatch,
    SkillSnapshotStale,
)
from tool.skill_tool import build_skill_tools_with_listing, skill_tool
from tool.tool import ToolContext


class MemoryProvider:
    def __init__(
        self,
        provider_id: str,
        rank: int,
        candidates: list[SkillCandidate] | None = None,
    ) -> None:
        self.id = provider_id
        self.rank = rank
        self.candidates = list(candidates or [])
        self.generation = 1
        self.complete = True
        self.available = True
        self.observe_calls = 0
        self.disposed = False
        self.gate: asyncio.Event | None = None

    async def revision(self, scope: ScopeKey) -> str:
        return f"r{self.generation}"

    async def observe(self, scope: ScopeKey) -> SkillProviderSnapshot:
        self.observe_calls += 1
        if self.gate is not None:
            await self.gate.wait()
        visible = [
            candidate
            for candidate in self.candidates
            if (
                (not candidate.scope.user_id or candidate.scope.user_id == scope.user_id)
                and (
                    not candidate.scope.project_id
                    or candidate.scope.project_id == scope.project_id
                )
                and (
                    not candidate.scope.workdir
                    or candidate.scope.workdir == scope.workdir
                )
            )
        ]
        return SkillProviderSnapshot(
            tuple(visible),
            self.complete,
            f"r{self.generation}",
            available=self.available,
        )

    async def list(self, scope: ScopeKey):
        return (await self.observe(scope)).candidates

    async def load(
        self,
        scope: ScopeKey,
        candidate: SkillCandidate,
        *,
        revision: str,
    ) -> SkillDefinition | None:
        if revision != f"r{self.generation}":
            raise SkillSnapshotStale("changed")
        return SkillDefinition(
            name=candidate.name,
            description=candidate.description,
            source=candidate.source,
            content=f"body:{self.id}:{candidate.stable_id}",
        )

    def invalidate(self, scope: ScopeKey | None = None) -> None:
        self.generation += 1

    async def dispose(self) -> None:
        self.disposed = True


def candidate(
    name: str,
    scope: ScopeKey,
    *,
    source: str = "test",
    stable_id: str | None = None,
) -> SkillCandidate:
    return SkillCandidate(
        name=name,
        description=f"description:{stable_id or name}",
        source=source,
        scope=scope,
        locator=stable_id or name,
        stable_id=stable_id or name,
    )


@pytest.mark.asyncio
async def test_nearest_scope_shadows_without_cross_user_or_project_leakage():
    global_provider = MemoryProvider(
        "global", 100, [candidate("same", ScopeKey(), stable_id="global")]
    )
    user_provider = MemoryProvider(
        "user",
        100,
        [
            candidate("same", ScopeKey(user_id="alice"), stable_id="alice"),
            candidate("same", ScopeKey(user_id="bob"), stable_id="bob"),
        ],
    )
    project_provider = MemoryProvider(
        "project",
        100,
        [
            candidate(
                "same",
                ScopeKey(user_id="alice", project_id="p1"),
                stable_id="alice-p1",
            ),
            candidate(
                "same",
                ScopeKey(user_id="alice", project_id="p2"),
                stable_id="alice-p2",
            ),
        ],
    )
    registry = SkillRegistry()
    registry.register(project_provider)
    registry.register(global_provider)
    registry.register(user_provider)

    alice_p1 = await registry.snapshot(
        ScopeKey(user_id="alice", project_id="p1", workdir="/workspace/alice/p1")
    )
    alice_p2 = await registry.snapshot(
        ScopeKey(user_id="alice", project_id="p2", workdir="/workspace/alice/p2")
    )
    bob = await registry.snapshot(
        ScopeKey(user_id="bob", project_id="p1", workdir="/workspace/bob/p1")
    )

    assert alice_p1.selection("same").candidate.stable_id == "alice-p1"
    assert alice_p2.selection("same").candidate.stable_id == "alice-p2"
    assert bob.selection("same").candidate.stable_id == "bob"
    assert alice_p1.revision != alice_p2.revision


@pytest.mark.asyncio
async def test_same_scope_uses_rank_then_provider_id_not_registration_order():
    scope = ScopeKey(user_id="u")
    zulu = MemoryProvider("zulu", 20, [candidate("same", scope, stable_id="z")])
    alpha = MemoryProvider("alpha", 20, [candidate("same", scope, stable_id="a")])
    ranked = MemoryProvider("ranked", 10, [candidate("rank", scope, stable_id="r")])
    later = MemoryProvider("later", 30, [candidate("rank", scope, stable_id="l")])
    registry = SkillRegistry()
    for provider in (zulu, later, alpha, ranked):
        registry.register(provider)

    snapshot = await registry.snapshot(scope)

    assert snapshot.selection("same").provider_id == "alpha"
    assert snapshot.selection("rank").provider_id == "ranked"
    assert sum(item.code == "skill_conflict" for item in snapshot.diagnostics) == 2


@pytest.mark.asyncio
async def test_incomplete_observation_never_replaces_last_known_good():
    scope = ScopeKey(user_id="u")
    provider = MemoryProvider("memory", 10, [candidate("old", scope)])
    registry = SkillRegistry(ttl_seconds=0)
    registry.register(provider)
    complete = await registry.snapshot(scope)
    assert complete.complete and [skill.name for skill in complete.skills] == ["old"]

    provider.candidates = [candidate("partial", scope)]
    provider.complete = False
    provider.available = False
    provider.generation += 1
    incomplete = await registry.snapshot(scope)

    assert not incomplete.complete and incomplete.stale and incomplete.available
    assert [skill.name for skill in incomplete.skills] == ["old"]


@pytest.mark.asyncio
async def test_cold_incomplete_provider_is_explicitly_unavailable():
    scope = ScopeKey(user_id="u")
    provider = MemoryProvider("memory", 10, [candidate("partial", scope)])
    provider.complete = False
    provider.available = False
    registry = SkillRegistry()
    registry.register(provider)

    snapshot = await registry.snapshot(scope)

    assert not snapshot.complete
    assert not snapshot.available
    assert snapshot.skills == ()


@pytest.mark.asyncio
async def test_revision_invalidation_refreshes_and_load_rejects_toctou():
    scope = ScopeKey(user_id="u")
    provider = MemoryProvider("memory", 10, [candidate("one", scope)])
    registry = SkillRegistry(ttl_seconds=60)
    registry.register(provider)
    old = await registry.snapshot(scope)

    provider.candidates = [candidate("two", scope)]
    registry.invalidate("memory", scope)
    new = await registry.snapshot(scope)

    assert [skill.name for skill in new.skills] == ["two"]
    with pytest.raises(SkillSnapshotStale):
        await registry.load(old, "one", scope=scope)


@pytest.mark.asyncio
async def test_model_loader_is_bound_to_the_advertised_provider_revision():
    scope = ScopeKey(user_id="u", project_id="p", workdir="/workspace/u/p")
    provider = MemoryProvider("memory", 10, [candidate("one", scope)])
    registry = SkillRegistry()
    registry.register(provider)
    tool, search = await build_skill_tools_with_listing(
        None,
        [],
        scope_key=scope,
        registry=registry,
    )
    assert search is None
    ctx = ToolContext(
        user_id="u",
        project_id="p",
        workdir="/workspace/u/p",
    )

    loaded = await tool.execute({"skill": "one"}, ctx)
    assert "body:memory:one" in loaded.output
    assert loaded.metadata["provider"] == "memory"

    provider.generation += 1
    stale = await tool.execute({"skill": "one"}, ctx)
    assert stale.metadata["error"] == "skill_snapshot_stale"


@pytest.mark.asyncio
async def test_concurrent_observers_share_one_inflight_collection():
    scope = ScopeKey(user_id="u")
    provider = MemoryProvider("memory", 10, [candidate("one", scope)])
    provider.gate = asyncio.Event()
    registry = SkillRegistry()
    registry.register(provider)

    reads = [asyncio.create_task(registry.snapshot(scope)) for _ in range(20)]
    await asyncio.sleep(0)
    provider.gate.set()
    snapshots = await asyncio.gather(*reads)

    assert provider.observe_calls == 1
    assert len({snapshot.revision for snapshot in snapshots}) == 1


@pytest.mark.asyncio
async def test_bounded_ttl_is_only_a_fallback_after_revision_key_hit():
    now = [0.0]
    scope = ScopeKey(user_id="u")
    provider = MemoryProvider("memory", 10, [candidate("one", scope)])
    registry = SkillRegistry(ttl_seconds=2, clock=lambda: now[0])
    registry.register(provider)

    await registry.snapshot(scope)
    await registry.snapshot(scope)
    assert provider.observe_calls == 1

    now[0] = 2.1
    await registry.snapshot(scope)
    assert provider.observe_calls == 2


@pytest.mark.asyncio
async def test_lkg_is_bounded_by_the_registry_cache_limit():
    provider = MemoryProvider(
        "memory", 10, [candidate("one", ScopeKey(), stable_id="global")]
    )
    registry = SkillRegistry(ttl_seconds=0, max_cache_entries=2)
    registry.register(provider)
    scopes = [ScopeKey(user_id=user) for user in ("a", "b", "c")]
    for scope in scopes:
        assert (await registry.snapshot(scope)).complete

    provider.complete = False
    provider.available = False
    provider.generation += 1
    evicted = await registry.snapshot(scopes[0])
    retained = await registry.snapshot(scopes[2])

    assert not evicted.available and evicted.skills == ()
    assert retained.available and retained.stale


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"name": "x" * 129},
        {"description": "x" * 502},
        {"source": "x" * 129},
        {"stable_id": "x" * 2_049},
        {"locator": {"payload": "x" * 4_097}},
        {"allowed_tools": tuple(f"tool-{index}" for index in range(65))},
        {"metadata": {"payload": "x" * 8_193}},
    ],
)
async def test_candidate_directory_fields_are_bounded(change):
    scope = ScopeKey(user_id="u")
    raw = candidate("valid", scope)
    bounded = replace(raw, **change)
    provider = MemoryProvider("memory", 10, [bounded])
    registry = SkillRegistry()
    registry.register(provider)

    snapshot = await registry.snapshot(scope)
    assert not snapshot.available
    assert snapshot.skills == ()
    assert any(item.code == "provider_snapshot_rejected" for item in snapshot.diagnostics)


@pytest.mark.asyncio
async def test_malformed_scoped_provider_fails_skill_capability_closed():
    scope = ScopeKey(user_id="u")
    provider = MemoryProvider(
        "memory",
        10,
        [replace(candidate("valid", scope), metadata={"payload": "x" * 9_000})],
    )
    registry = SkillRegistry()
    registry.register(provider)

    tools = await attach_skill_listing(
        {"skill": skill_tool},
        None,
        [],
        scope_key=scope,
        skill_registry=registry,
    )

    assert "skill" not in tools


@pytest.mark.asyncio
async def test_malformed_refresh_keeps_warm_lkg_instead_of_partial_view():
    scope = ScopeKey(user_id="u")
    provider = MemoryProvider("memory", 10, [candidate("old", scope)])
    registry = SkillRegistry(ttl_seconds=0)
    registry.register(provider)
    assert (await registry.snapshot(scope)).complete

    provider.candidates = [
        replace(candidate("new", scope), metadata={"payload": "x" * 9_000})
    ]
    provider.generation += 1
    stale = await registry.snapshot(scope)

    assert stale.available and stale.stale and not stale.complete
    assert [skill.name for skill in stale.skills] == ["old"]


@pytest.mark.asyncio
async def test_unregister_and_registry_dispose_are_isolated_and_idempotent():
    one = MemoryProvider("one", 10)
    two = MemoryProvider("two", 10)
    first = SkillRegistry()
    second = SkillRegistry()
    unregister = first.register(one)
    second.register(two)

    await unregister()
    await unregister()
    assert one.disposed and first.provider_ids == ()
    assert second.provider_ids == ("two",)

    await first.dispose()
    await first.dispose()
    await second.dispose()
    assert two.disposed


@dataclass(frozen=True)
class _State:
    availability: str
    snapshot: dict | None


class ScopedSandbox:
    def __init__(self, user_id: str):
        self.user_scope = user_scope_for_identity(user_id)
        self.generation = "g1"

    async def get_catalogue_projection_state(self):
        return _State(
            "available",
            {
                "skills_generation": self.generation,
                "skills": [
                    {
                        "name": "remote",
                        "description": "remote skill",
                        "source": "container",
                        "install_dir": "remote",
                        "package_digest": "d1",
                    }
                ],
            },
        )

    async def get_skill(self, name: str):
        return {
            "name": "remote",
            "description": "remote skill",
            "source": "container",
            "install_dir": "remote",
            "content": "remote body",
            "base_dir": f"/data/skills/{self.user_scope}/remote",
            "files": [],
        }


@pytest.mark.asyncio
async def test_sandbox_provider_refuses_cross_tenant_observe_and_load():
    sandbox = ScopedSandbox("alice")
    provider = SandboxCatalogueSkillProvider(sandbox)
    registry = SkillRegistry()
    registry.register(provider)
    alice = ScopeKey(user_id="alice")
    snapshot = await registry.snapshot(alice)
    assert snapshot.available and [skill.name for skill in snapshot.skills] == ["remote"]

    with pytest.raises(SkillScopeMismatch):
        await registry.load(snapshot, "remote", scope=ScopeKey(user_id="bob"))

    bob = await registry.snapshot(ScopeKey(user_id="bob"))
    assert not bob.available and bob.skills == ()


@pytest.mark.asyncio
async def test_legacy_unscoped_sandbox_object_binds_to_its_first_user():
    sandbox = ScopedSandbox("alice")
    sandbox.user_scope = ""
    provider = SandboxCatalogueSkillProvider(sandbox)

    assert (await provider.observe(ScopeKey(user_id="alice"))).available
    with pytest.raises(SkillScopeMismatch):
        await provider.observe(ScopeKey(user_id="bob"))


@pytest.mark.asyncio
async def test_personal_library_is_a_ranked_user_provider_not_an_ad_hoc_merge():
    sandbox = ScopedSandbox("alice")
    remote = SandboxCatalogueSkillProvider(sandbox)

    async def owned(user_id: str):
        assert user_id == "alice"
        return [
            {
                "id": "library-1",
                "name": "remote",
                "install_dir": "remote",
                "version": 3,
                "lifecycle_generation": 1,
            }
        ]

    personal = PersonalLibrarySkillProvider(remote, list_owned=owned)
    registry = SkillRegistry()
    registry.register(remote)
    registry.register(personal)

    snapshot = await registry.snapshot(ScopeKey(user_id="alice"))

    assert snapshot.selection("remote").provider_id == "personal-user-library"
    assert snapshot.skills[0].source == "personal"
    assert any(item.code == "skill_conflict" for item in snapshot.diagnostics)


def _write_skill(root: Path, description: str) -> None:
    target = root / ".openbox" / "skills" / "demo"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        f"---\nname: demo\ndescription: {description}\n---\n{description}",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_project_provider_uses_explicit_workdir_not_process_cwd(tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write_skill(one, "one")
    _write_skill(two, "two")
    provider = HostFilesystemSkillProvider("project", 10, project=True)
    registry = SkillRegistry()
    registry.register(provider)
    original = os.getcwd()
    os.chdir(elsewhere)
    try:
        first = await registry.snapshot(
            ScopeKey(user_id="u", project_id="p1", workdir=str(one))
        )
        second = await registry.snapshot(
            ScopeKey(user_id="u", project_id="p2", workdir=str(two))
        )
    finally:
        os.chdir(original)

    assert first.skills[0].description == "one"
    assert second.skills[0].description == "two"


@pytest.mark.asyncio
async def test_host_observe_rechecks_revision_after_reading_candidates(
    tmp_path, monkeypatch
):
    import skill.provider as provider_module

    root = tmp_path / "project"
    _write_skill(root, "first")
    skill_md = root / ".openbox" / "skills" / "demo" / "SKILL.md"
    original_parse = provider_module.parse_frontmatter
    changed = False

    def mutate_after_read(content: str):
        nonlocal changed
        parsed = original_parse(content)
        if not changed:
            changed = True
            skill_md.write_text(
                "---\nname: demo\ndescription: second\n---\nsecond",
                encoding="utf-8",
            )
        return parsed

    monkeypatch.setattr(provider_module, "parse_frontmatter", mutate_after_read)
    provider = HostFilesystemSkillProvider("project", 10, project=True)
    scope = ScopeKey(user_id="u", project_id="p", workdir=str(root))

    observed = await provider.observe(scope)

    assert not observed.complete
    assert any(item.code == "host_revision_raced" for item in observed.diagnostics)
