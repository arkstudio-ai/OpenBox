"""One private Wuying resolution shares metadata, never current authority.

Real SQL/Driver/SkillRegistry/client and durable resource gate; only the
existing guest HTTP/body is an explicit local fixture. No provider/cloud IO.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text

from agent.driver import bind_current_lease, reserve_run, reset_current_lease
from agent.tool_resolution import resolve_step_tools
from db.base import get_db_session, get_engine
from db.models.external_effect import ExternalEffect
from db.models.workspace import WorkspaceMember
from sandbox.client import CatalogueResolutionExpired
from sandbox import runtime_operation
from skill.provider import ScopeKey, SkillSnapshotStale, skill_registry_for
from tool.skill_tool import skill_tool, skill_search_tool
from tests.unit.test_private_wuying_manager import manager_world  # noqa: F401
from tests.unit.test_private_wuying_runtime import assistant_database, wuying_world  # noqa: F401
from tests.unit.test_action_server_desktop_lease import server  # noqa: F401
from resource_gate import Fence, ResourceGate


@pytest.fixture
async def catalogue(manager_world, tmp_path, monkeypatch):
    w = manager_world
    w.client = await w.manager.get_client(w.session.id, user_id=w.owner)
    w.lease = await reserve_run(w.session.id, w.owner)
    token = bind_current_lease(w.lease)
    w.scope = ScopeKey(w.owner, w.session.project_id or "", str(tmp_path))
    w.gate = ResourceGate(tmp_path / "directory-journal.sqlite3")
    w.generation, w.reads, w.checks, w.clock = "one", [], [], 0.0
    w.order_reads = False
    w.client._catalogue_clock = lambda: w.clock
    original_context = runtime_operation.runtime_context

    async def current(*args, **kwargs):
        result = await original_context(*args, **kwargs)
        w.checks.append(result)
        # Model expensive source validation without a wall-clock sleep: each
        # real validation outlives the unchanged two-second metadata TTL.
        w.clock += 3.0
        return result

    monkeypatch.setattr(runtime_operation, "runtime_context", current)
    # Give the original successful reads a deterministic send order. Otherwise
    # the existing unresolved-effect guard may reject one concurrent registry
    # getter before it sends, obscuring the repeated successful-read count.
    # The real ledger, current SQL checks and resource gate still run in full.
    original_read = runtime_operation.read_runtime_catalogue
    ordered_reads = asyncio.Lock()

    async def ordered(*args, **kwargs):
        if not w.order_reads:
            return await original_read(*args, **kwargs)
        async with ordered_reads:
            return await original_read(*args, **kwargs)

    monkeypatch.setattr(runtime_operation, "read_runtime_catalogue", ordered)
    monkeypatch.setattr("skill.skill._skill_dirs", lambda: [])
    from tool import registry
    monkeypatch.setitem(registry._tools, "skill", skill_tool)
    monkeypatch.setitem(registry._tools, "skill_search", skill_search_tool)
    original_remote = w.remote_handler

    async def remote(request):
        path = request.url.path.removeprefix(httpx.URL(w.client.base_url).path)
        if path == "/resource-control/status":
            return w.gate.status()
        if path == "/resource-control/bind":
            body = json.loads(request.content)
            return w.gate.bind(Fence(**{k: body[k] for k in ("resource_id", "epoch", "owner_kind", "owner_id")}),
                body["command_id"], body["journal_id"])
        if path == "/catalog":
            operation = w.gate.admit(request.headers, request.method, request.url.path)
            w.gate.checkpoint(operation)
            w.reads.append(w.generation)
            result = {"generation": w.generation, "skills_generation": w.generation,
                "skills": [{"name": "fixture-" + w.generation, "description": "directory metadata"}],
                "mcp_tools": [], "mcp_resources": []}
            w.gate.finish(operation)
            return httpx.Response(200, json=result, headers={
                "etag": '"' + w.generation + '"',
                "X-OpenBox-Remote-Operation": operation["id"],
                "X-OpenBox-Resource-Journal": operation["journal_id"]})
        return await original_remote(request)

    w.remote_handler = remote
    try:
        yield w
    finally:
        reset_current_lease(token)
        await w.lease.release(session_status="idle")


async def resolve(w):
    return await resolve_step_tools(SimpleNamespace(name="build", tools=["skill", "skill_search"], permission=[]),
        w.client, [], scope_key=w.scope, return_catalogue_state=True)


@pytest.mark.parametrize("pinned,ordered", [(False, True), (True, True), (True, False)],
    ids=["original-five-reads", "one-resolution", "concurrent-registry"])
async def test_real_registry_resolution_reuses_only_metadata_and_next_resolution_is_fresh(
        catalogue, monkeypatch, record_property, pinned, ordered):
    w = catalogue
    w.order_reads = ordered
    if not pinned:
        @asynccontextmanager
        async def original(_scope):
            yield
        monkeypatch.setattr(w.client, "catalogue_resolution_scope", original)
    original_registry = skill_registry_for(w.client)
    first = await resolve(w)
    assert first.catalogue_availability == "available" and "fixture-one" in first.tools["skill"].description
    expected = 1 if pinned else 5
    assert len(w.reads) == expected and len(w.checks) >= 5
    assert skill_registry_for(w.client) is original_registry
    record_property("first_resolution", json.dumps({"catalogue_gets": len(w.reads), "fresh_contexts": len(w.checks)}))
    w.generation = "two"
    second = await resolve(w)
    assert "fixture-two" in second.tools["skill"].description and "fixture-one" not in second.tools["skill"].description
    assert len(w.reads) == expected * 2
    async with get_db_session() as db:
        effects = list((await db.scalars(select(ExternalEffect).where(
            ExternalEffect.session_id == w.session.id, ExternalEffect.operation == "catalogue_read"))).all())
        assert len(effects) == len(w.reads) and all(e.state == "succeeded" and e.attempt_count == 1 for e in effects)


async def test_scope_exit_keeps_real_skill_load_version_checks(catalogue):
    w = catalogue
    registry = skill_registry_for(w.client)
    async with w.client.catalogue_resolution_scope(w.scope):
        await w.client.get_catalogue_projection_state()
        snapshot = await registry.snapshot(w.scope)
    w.generation = "changed-after-selection"
    with pytest.raises(SkillSnapshotStale):
        await registry.load(snapshot, "fixture-one", scope=w.scope)
    assert w.reads == ["one", "changed-after-selection"]
    assert w.client._catalogue_resolution.get() is None


@pytest.mark.parametrize("change", ["epoch", "route", "driver", "scope"])
async def test_pin_identity_changes_fail_closed_without_another_get(catalogue, change):
    w = catalogue
    with pytest.raises(CatalogueResolutionExpired):
        async with w.client.catalogue_resolution_scope(w.scope):
            assert (await w.client.get_catalogue_projection_state()).availability == "available"
            if change == "epoch":
                w.client._invalidate_catalogue_cache()
            elif change == "route":
                w.client.private_runtime_route = replace(w.client.private_runtime_route,
                    guest_attempt_id="replacement-attempt")
            elif change == "driver":
                replacement = replace(w.lease, generation=w.lease.generation + 1)
                token = bind_current_lease(replacement)
                try:
                    await w.client.get_catalogue_projection_state()
                finally:
                    reset_current_lease(token)
            else:
                async with w.client.catalogue_resolution_scope(replace(w.scope, project_id="other-project")):
                    pytest.fail("A nested resolution borrowed another scope")
            await w.client.get_catalogue_projection_state()
    assert w.reads == ["one"] and w.client._catalogue_resolution.get() is None


@pytest.mark.parametrize("cancel", [False, True])
async def test_inherited_shield_child_cannot_reuse_scope_after_exit(catalogue, cancel):
    w = catalogue
    release, ready = asyncio.Event(), asyncio.Event()
    child = None

    async def later():
        await release.wait()
        return await w.client.get_catalogue_projection_state()

    async def parent():
        nonlocal child
        async with w.client.catalogue_resolution_scope(w.scope):
            await w.client.get_catalogue_projection_state()
            child = asyncio.create_task(later())
            ready.set()
            if cancel:
                await asyncio.shield(child)

    task = asyncio.create_task(parent())
    await asyncio.wait_for(ready.wait(), 3)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    release.set()
    with pytest.raises(CatalogueResolutionExpired):
        await child
    assert w.reads == ["one"] and w.client._catalogue_resolution.get() is None


async def test_independent_membership_revocation_rejects_pinned_metadata(catalogue):
    w = catalogue
    async with get_db_session() as held:
        original = await held.get(WorkspaceMember, (w.workspace, w.owner))
        assert original.status == "active"
        reader_pid = await held.scalar(text("select pg_backend_pid()")) if get_engine().dialect.name == "postgresql" else None
        with pytest.raises(CatalogueResolutionExpired):
            async with w.client.catalogue_resolution_scope(w.scope):
                await w.client.get_catalogue_projection_state()
                async with get_db_session() as writer:
                    if reader_pid:
                        assert await writer.scalar(text("select pg_backend_pid()")) != reader_pid
                    member = await writer.get(WorkspaceMember, (w.workspace, w.owner))
                    member.status = "removed"
                assert original.status == "active"  # stale held ORM cannot authorize this getter
                await w.client.get_catalogue_projection_state()
    assert w.reads == ["one"]
