"""A single step's Wuying catalogue data is projected, never its authority.

Real SQL, Driver, runtime_context, resource journal, and registry. Only guest
HTTP is a local in-process transport; no cloud or provider access is allowed.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, select, text

from agent.tool_resolution import resolve_step_tools
from db.base import get_db_session, get_engine
from db.models.agent_driver import AgentDriverState
from db.models.private_runtime import PrivateRuntimeBinding
from db.models.resource_control import ResourceControlLease
from db.models.session import Session
from db.models.user_skill import UserSkill
from db.models.workspace import WorkspaceMember
from sandbox import runtime_operation
from sandbox.client import CatalogueResolutionExpired
from skill.provider import (
    PersonalLibrarySkillProvider, SandboxCatalogueSkillProvider, SkillSnapshotStale,
    skill_registry_for,
)
from tests.unit.test_private_catalogue_resolution import (
    assistant_database, catalogue, catalogue_effects, change_catalogue_authority,
    manager_world, resolve, server, wuying_world,
)  # noqa: F401


async def test_one_step_observation_keeps_io_guards_and_fresh_publication(
        catalogue, monkeypatch, record_property):
    w = catalogue
    gets, selects = [], []
    original = w.client.get_catalogue_projection_state

    async def getter():
        gets.append(True)
        return await original()

    def query(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    monkeypatch.setattr(w.client, "get_catalogue_projection_state", getter)
    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", query)
    try:
        result = await resolve(w)
    finally:
        event.remove(engine, "before_cursor_execute", query)
    assert result.catalogue_availability == "available"
    assert "fixture-one" in result.tools["skill"].description
    counts = {"catalogue_getters": len(gets), "runtime_contexts": len(w.checks),
        "catalogue_http": len(w.reads), "selects": len(selects)}
    record_property("single_observation_counts", json.dumps(counts))
    assert counts["catalogue_http"] == 1
    assert counts["catalogue_getters"] == 1, counts
    assert counts["runtime_contexts"] == 5, counts


def providers(w):
    registry = skill_registry_for(w.client)
    remote = next(p for p in registry._providers.values() if type(p) is SandboxCatalogueSkillProvider)
    personal = next(p for p in registry._providers.values() if type(p) is PersonalLibrarySkillProvider)
    return registry, remote, personal


def pause_owned(w, monkeypatch, *, at=1):
    registry, remote, personal = providers(w)
    entered, release = asyncio.Event(), asyncio.Event()
    original = personal._read_owned
    count = 0

    async def read(scope):
        nonlocal count
        result = await original(scope)  # real independent ownership SELECT
        count += 1
        if count == at:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(personal, "_read_owned", read)
    return registry, remote, personal, entered, release


@asynccontextmanager
async def step_view(w):
    async with w.client.catalogue_resolution_scope(w.scope):
        await w.client.get_catalogue_projection_state()
        view = skill_registry_for(w.client).step_catalogue_view(
            w.client._step_catalogue_observation(w.scope), w.scope)
        try:
            yield view
        finally:
            view.close()


@pytest.mark.parametrize("change", ["membership", "generation", "resource", "binding"])
async def test_publication_rechecks_independent_sql_after_owned_read_and_cannot_be_swallowed(
        catalogue, monkeypatch, record_property, change):
    w = catalogue
    registry, remote, personal, entered, release = pause_owned(w, monkeypatch, at=2)
    rejected = []
    original = runtime_operation.runtime_context

    async def checked(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except Exception as exc:
            rejected.append(getattr(exc, "code", type(exc).__name__))
            raise

    monkeypatch.setattr(runtime_operation, "runtime_context", checked)
    async with get_db_session() as held:
        stale_member = await held.get(WorkspaceMember, (w.workspace, w.owner))
        reader_pid = await held.scalar(text("select pg_backend_pid()")) if get_engine().dialect.name == "postgresql" else None
        pending = asyncio.create_task(resolve(w))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert not registry._cache and not registry._lkg
            assert not remote._states and not personal._owned_cache
            if change == "binding":
                async with get_db_session() as writer:
                    writer_pid = await writer.scalar(text("select pg_backend_pid()")) if reader_pid else None
                    if reader_pid:
                        assert writer_pid != reader_pid
                    row = await writer.get(PrivateRuntimeBinding, w.client.private_runtime_route.binding_id)
                    row.revision += 1
            else:
                writer_pid = await change_catalogue_authority(w, change, w.checks[-1][1], reader_pid)
            record_property("independent_writer", json.dumps({"reader_pid": reader_pid, "writer_pid": writer_pid}))
            assert stale_member.status == "active"
            release.set()
            # attach_skill_listing catches the exact source/lease exception;
            # the closed pin must still make the complete resolution fail.
            with pytest.raises(CatalogueResolutionExpired):
                await pending
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    assert rejected == [{"membership": "ASSISTANT_WORKSPACE_FORBIDDEN", "generation": "LeaseLostError",
                         "resource": "RESOURCE_CONTROL_HELD", "binding": "RESOURCE_CONTROL_HELD"}[change]]
    assert not registry._cache and not registry._lkg and not remote._states and not personal._owned_cache
    effect, = await catalogue_effects(w)
    assert effect.state == "succeeded" and effect.attempt_count == 1 and w.reads == ["one"]


@pytest.mark.parametrize("change", ["route", "nested_route", "client_epoch", "close", "lease_abort", "provider", "registry"])
async def test_mutable_identity_cannot_publish_after_owned_await(catalogue, monkeypatch, change):
    w = catalogue
    registry, remote, personal, entered, release = pause_owned(w, monkeypatch)
    pending = asyncio.create_task(resolve(w))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if change == "route":
            w.client.private_runtime_route = replace(w.client.private_runtime_route, revision=999)
        elif change == "nested_route":
            w.client.private_runtime_route.provider_identity["guest_binding"]["attempt_id"] = "replaced"
        elif change == "client_epoch":
            w.client._invalidate_catalogue_cache()
        elif change == "close":
            await w.client.aclose()
        elif change == "lease_abort":
            w.lease.abort.set()
        elif change == "provider":
            remote.invalidate(w.scope)
        else:
            registry.invalidate(notify_provider=False)
        release.set()
        with pytest.raises(CatalogueResolutionExpired):
            await pending
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert not registry._cache and not registry._lkg and not remote._states and not personal._owned_cache
    assert w.reads == ["one"] and w.client._catalogue_resolution.get() is None


async def test_owned_wait_holds_no_session_driver_binding_or_resource_locks(catalogue, monkeypatch):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent PostgreSQL row-lock proof")
    w = catalogue
    _, _, _, entered, release = pause_owned(w, monkeypatch)
    pending = asyncio.create_task(resolve(w))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with get_db_session() as db:
            await db.execute(text("SET LOCAL lock_timeout = '500ms'"))
            for column, value in ((Session.id, w.session.id), (AgentDriverState.session_id, w.session.id),
                    (PrivateRuntimeBinding.id, w.client.private_runtime_route.binding_id),
                    (ResourceControlLease.id, w.checks[-1][1].resource_id)):
                assert await db.scalar(select(column).where(column == value).with_for_update(nowait=True)) == value
        release.set()
        assert "fixture-one" in (await pending).tools["skill"].description
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("late_child", [False, True])
async def test_cancelled_or_exited_scope_cannot_publish_late_collection(catalogue, monkeypatch, late_child):
    w = catalogue
    registry, remote, personal, entered, release = pause_owned(w, monkeypatch, at=2)
    child = None

    async def build():
        nonlocal child
        async with step_view(w) as view:
            if late_child:
                child = asyncio.create_task(view.snapshot(w.scope))
                await entered.wait()
                return
            return await view.snapshot(w.scope)

    task = asyncio.create_task(build())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if late_child:
            await task
            release.set()
            with pytest.raises(CatalogueResolutionExpired):
                await child
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
        assert not registry._inflight and not registry._cache and not registry._lkg
        assert not remote._states and not personal._owned_cache
        assert w.reads == ["one"]
    finally:
        release.set()
        for pending in (task, child):
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("revoke", [False, True])
async def test_hot_registry_still_freshly_publishes_without_collect(catalogue, monkeypatch, revoke):
    w = catalogue
    registry, remote, personal = providers(w)
    registry._ttl_seconds = 120
    w.client._catalogue_ttl_seconds = 120
    await resolve(w)
    lkg = registry._lkg[w.scope]
    collected = []
    original = registry._collect

    async def collect(*args, **kwargs):
        collected.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(registry, "_collect", collect)
    _, _, _, entered, release = pause_owned(w, monkeypatch)
    w.checks.clear()
    pending = asyncio.create_task(resolve(w))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if revoke:
            async with get_db_session() as writer:
                (await writer.get(WorkspaceMember, (w.workspace, w.owner))).status = "removed"
        release.set()
        if revoke:
            with pytest.raises(CatalogueResolutionExpired):
                await pending
        else:
            assert "fixture-one" in (await pending).tools["skill"].description
        assert collected == [] and w.reads == ["one"]
        assert len(w.checks) == (1 if revoke else 2)  # actual TTL getter + final fresh
        assert registry._lkg[w.scope] is lkg
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_successful_body_load_after_view_closed_uses_original_client_and_versions(catalogue):
    w = catalogue
    async with step_view(w) as view:
        snapshot = await view.snapshot(w.scope)
    assert view.closed and w.client._catalogue_resolution.get() is None
    original = w.remote_handler
    body_requests = []

    async def remote(request):
        if request.url.path.endswith("/skills/fixture-one"):
            operation = w.gate.admit(request.headers, request.method, request.url.path)
            w.gate.checkpoint(operation)
            body_requests.append(request.url.path)
            w.gate.finish(operation)
            return httpx.Response(200, json={"name": "fixture-one", "content": "isolated fixture body"}, headers={
                "X-OpenBox-Remote-Operation": operation["id"],
                "X-OpenBox-Resource-Journal": operation["journal_id"]})
        return await original(request)

    w.remote_handler = remote

    async def load():
        body = await view.load(snapshot, "fixture-one", scope=w.scope)
        return {"name": body.name, "content": body.content}

    result = await runtime_operation.run_runtime_operation(w.client, session_id=w.session.id, user_id=w.owner,
        stage="skill_body_fixture", key="body", payload={"name": "fixture-one"}, operation=load)
    assert result == {"name": "fixture-one", "content": "isolated fixture body"}
    assert len(body_requests) == 1 and len(w.reads) > 1
    w.generation = "two"
    with pytest.raises(SkillSnapshotStale):
        await view.load(snapshot, "fixture-one", scope=w.scope)
    assert len(body_requests) == 1


async def test_two_real_owned_versions_keep_lkg_on_revision_race(catalogue, monkeypatch):
    w = catalogue
    now = datetime.now(timezone.utc)
    row_id = "library-" + uuid4().hex
    async with get_db_session() as db:
        db.add(UserSkill(id=row_id, owner_id=w.owner, workspace_id=w.workspace,
            name="fixture-one", install_dir="fixture-one", archive_data=b"synthetic",
            archive_sha256="0" * 64, archive_size=9, created_at=now, updated_at=now))
    registry, remote, personal = providers(w)
    await resolve(w)
    old = registry._lkg[w.scope]
    assert old.selection("fixture-one").provider_id == personal.id
    registry._cache.clear()  # only metadata TTL/cache; authority remains real
    original = personal._read_owned
    count = 0

    async def read(scope):
        nonlocal count
        result = await original(scope)
        count += 1
        if count == 1:
            async with get_db_session() as writer:
                (await writer.get(UserSkill, row_id)).version += 1
        return result

    monkeypatch.setattr(personal, "_read_owned", read)
    async with step_view(w) as view:
        result = await view.snapshot(w.scope)
    assert count == 2 and result.stale and not result.complete
    assert result.skills == old.skills and registry._lkg[w.scope] is old
    assert any(item.code == "provider_revision_raced" for item in result.diagnostics)


@pytest.mark.parametrize("warm", [False, True])
async def test_transport_outage_keeps_unavailable_or_lkg_without_replay(catalogue, warm):
    w = catalogue
    registry, _, _ = providers(w)
    old = None
    if warm:
        await resolve(w)
        old = registry._lkg[w.scope]
    original = w.remote_handler

    async def lost(request):
        result = await original(request)
        if request.url.path.endswith("/catalog"):
            raise httpx.ReadTimeout("isolated original response lost", request=request)
        return result

    w.remote_handler = lost
    result = await resolve(w)
    assert result.catalogue_availability == "unavailable"
    assert ("skill" in result.tools) == warm
    if warm:
        assert "fixture-one" in result.tools["skill"].description and registry._lkg[w.scope] is old
    else:
        assert not registry._lkg
    before = len(w.reads)
    again = await resolve(w)
    assert again.catalogue_availability == "unavailable" and len(w.reads) == before
    assert (await catalogue_effects(w))[-1].state == "outcome_unknown"


async def test_no_skill_branch_retains_original_context_count(catalogue):
    w = catalogue
    result = await resolve_step_tools(SimpleNamespace(name="build", tools=["skill_search"], permission=[]),
        w.client, [], scope_key=w.scope, return_catalogue_state=True)
    assert result.tools == {} and len(w.checks) == 4 and w.reads == ["one"]


async def test_concurrent_new_observation_is_not_overwritten_by_older_scope_delta(catalogue, monkeypatch):
    w = catalogue
    registry, remote, personal, entered, release = pause_owned(w, monkeypatch)
    old = asyncio.create_task(resolve(w))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not registry._lkg and not registry._cache and not remote._states
        w.generation = "two"
        newer = await resolve(w)  # a separate explicit scope, with its own actual GET/gates
        assert "fixture-two" in newer.tools["skill"].description
        lkg, cache = registry._lkg[w.scope], dict(registry._cache)
        remote_state, owned_state = remote._states[w.scope.user_scope()], personal._owned_cache[w.scope.user_scope()]
        release.set()
        older = await old
        assert "fixture-one" in older.tools["skill"].description
        assert registry._lkg[w.scope] is lkg and dict(registry._cache) == cache
        assert remote._states[w.scope.user_scope()] is remote_state
        assert personal._owned_cache[w.scope.user_scope()] is owned_state
        assert w.reads == ["one", "two"] and not registry._inflight
    finally:
        release.set()
        if not old.done():
            old.cancel()
            await asyncio.gather(old, return_exceptions=True)


async def test_observation_requires_original_scope_and_detaches_mutable_metadata(catalogue):
    w = catalogue
    registry, _, _ = providers(w)
    assert w.client._step_catalogue_observation(w.scope) is None
    with pytest.raises(TypeError):
        registry.step_catalogue_view({"skills": []}, w.scope)
    async with w.client.catalogue_resolution_scope(w.scope):
        await w.client.get_catalogue_projection_state()
        observation = w.client._step_catalogue_observation(w.scope)
        observation.state.snapshot["skills"][0]["name"] = "not-from-the-guest"
        with pytest.raises(CatalogueResolutionExpired):
            registry.step_catalogue_view(observation, replace(w.scope, workdir="/other"))
        view = registry.step_catalogue_view(observation, w.scope)
        result = await view.snapshot(w.scope)
        assert [row.name for row in result.skills] == ["fixture-one"]
        view.close()
    with pytest.raises(CatalogueResolutionExpired):
        observation.check()


async def test_cancelled_host_that_finishes_late_cannot_publish(catalogue, monkeypatch):
    w = catalogue
    registry, remote, personal = providers(w)
    host = registry._providers["host-global"]
    original = host.observe
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stubborn(scope):
        result = await original(scope)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()  # model a late local filesystem worker
        return result

    monkeypatch.setattr(host, "observe", stubborn)
    pending = asyncio.create_task(resolve(w))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        await asyncio.wait_for(cancelled.wait(), 5)
        assert not registry._cache and not registry._lkg and not remote._states and not personal._owned_cache
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not registry._cache and not registry._lkg and not remote._states and not personal._owned_cache
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_custom_remote_provider_is_not_replaced_based_on_its_id(catalogue, monkeypatch):
    w = catalogue
    registry, remote, _ = providers(w)
    calls = []

    class CustomRemote(SandboxCatalogueSkillProvider):
        async def revision(self, scope):
            calls.append("revision")
            return await super().revision(scope)

        async def observe(self, scope):
            calls.append("observe")
            return await super().observe(scope)

    custom = CustomRemote(w.client, provider_id=remote.id)
    await registry.unregister("personal-user-library")
    await registry.unregister(remote.id)
    registry.register(custom)
    result = await resolve(w)
    assert "fixture-one" in result.tools["skill"].description
    assert calls == ["revision", "observe"] and len(w.checks) == 7


async def test_ordinary_client_uses_original_registry_getter_contract(monkeypatch):
    from skill.provider import ScopeKey
    from tool.skill_tool import skill_search_tool, skill_tool
    from tool import registry
    from sandbox.client import CatalogueProjectionState

    class OrdinaryClient:
        user_scope = ""

        def __init__(self):
            self.gets = 0

        async def get_catalogue_projection_state(self):
            self.gets += 1
            return CatalogueProjectionState("available", {"generation": "ordinary", "skills": [
                {"name": "ordinary", "description": "unchanged ordinary fixture"}],
                "mcp_tools": [], "mcp_resources": []})

    monkeypatch.setattr("agent.tool_payload._proxy_encoding", lambda: None)
    monkeypatch.setattr("skill.skill._skill_dirs", lambda: [])
    monkeypatch.setitem(registry._tools, "skill", skill_tool)
    monkeypatch.setitem(registry._tools, "skill_search", skill_search_tool)
    client = OrdinaryClient()
    result = await resolve_step_tools(SimpleNamespace(name="build", tools=["skill"], permission=[]),
        client, [], scope_key=ScopeKey("ordinary-user"))
    assert "ordinary" in result["skill"].description and client.gets == 5
