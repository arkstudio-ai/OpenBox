"""One private Wuying resolution shares metadata, never current authority.

Real SQL/Driver/SkillRegistry/client and durable resource gate; only the
existing guest HTTP/body is an explicit local fixture. No provider/cloud IO.
"""
import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event, select, text

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
    pytest.skip("Dormant private actor runtime: its catalogue reads run as Driver runtime preparation, which "
        "refuses the assistant main conversation, and that conversation is the only private audience left. "
        "Retained until the follow-up removes sandbox.private_runtime/private_wuying.")
    w = manager_world
    # Catalogue authority is independent of optional tokenizer downloads.
    # Exercise the real local proxy fallback without allowing external IO.
    monkeypatch.setattr("agent.tool_payload._proxy_encoding", lambda: None)
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


@pytest.mark.parametrize("cache", ["pin", "ttl"])
async def test_independent_membership_revocation_rejects_pinned_metadata(catalogue, cache):
    w = catalogue
    w.client._catalogue_ttl_seconds = 120
    scope = w.client.catalogue_resolution_scope(w.scope) if cache == "pin" else nullcontext()
    async with get_db_session() as held:
        original = await held.get(WorkspaceMember, (w.workspace, w.owner))
        assert original.status == "active"
        reader_pid = await held.scalar(text("select pg_backend_pid()")) if get_engine().dialect.name == "postgresql" else None
        from assistant.policy import AssistantError
        with pytest.raises(CatalogueResolutionExpired if cache == "pin" else AssistantError):
            async with scope:
                await w.client.get_catalogue_projection_state()
                async with get_db_session() as writer:
                    if reader_pid:
                        assert await writer.scalar(text("select pg_backend_pid()")) != reader_pid
                    member = await writer.get(WorkspaceMember, (w.workspace, w.owner))
                    member.status = "removed"
                assert original.status == "active"  # stale held ORM cannot authorize this getter
                if cache == "pin":
                    await w.client.get_catalogue_projection_state()
                else:
                    await w.client.get_catalogue_projection()
    assert w.reads == ["one"]


async def catalogue_effects(w):
    async with get_db_session() as db:
        return list((await db.scalars(select(ExternalEffect).where(
            ExternalEffect.session_id == w.session.id, ExternalEffect.operation == "catalogue_read")
            .order_by(ExternalEffect.created_at))).all())


@pytest.mark.parametrize("cache", ["cold", "expired"])
async def test_certain_private_miss_keeps_four_fresh_contexts_and_its_own_effect(
        catalogue, record_property, cache):
    w = catalogue
    # Finish actual SQL enrollment/remote bind before measuring the ready path.
    await runtime_operation.runtime_context(w.client, w.lease)
    if cache == "expired":
        assert (await w.client.get_catalogue_projection_state()).availability == "available"
        w.clock = w.client._catalogue_cache.expires_at + 1
    prior_reads, prior_effects = len(w.reads), len(await catalogue_effects(w))
    w.checks.clear()
    counts = {"select": 0, "other": 0}

    def query(_connection, _cursor, statement, _parameters, _context, _many):
        counts["select" if statement.lstrip().upper().startswith("SELECT") else "other"] += 1

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", query)
    try:
        state = await w.client.get_catalogue_projection_state()
    finally:
        event.remove(engine, "before_cursor_execute", query)
    record_property("certain_miss_counts", json.dumps({
        "cache": cache, "runtime_contexts": len(w.checks), "sql": counts,
        "catalogue_gets": len(w.reads) - prior_reads,
    }))
    assert state.availability == "available" and state.snapshot["generation"] == "one"
    assert len(w.checks) == 4
    assert len(w.reads) == prior_reads + 1
    effects = await catalogue_effects(w)
    assert len(effects) == prior_effects + 1
    assert effects[-1].state == "succeeded" and effects[-1].attempt_count == 1
    assert effects[-1].provider_receipt["result"] == {"observed": True}
    assert all(check[1:3] == w.checks[0][1:3] for check in w.checks)


@pytest.mark.parametrize("cache", ["ttl", "pin"])
async def test_private_cache_hit_still_runs_its_own_current_context(catalogue, cache):
    w = catalogue
    w.client._catalogue_ttl_seconds = 120
    scope = w.client.catalogue_resolution_scope(w.scope) if cache == "pin" else nullcontext()
    async with scope:
        assert (await w.client.get_catalogue_projection_state()).availability == "available"
        before = len(w.reads), len(await catalogue_effects(w))
        if cache == "pin":
            w.clock = w.client._catalogue_cache.expires_at + 1
        w.checks.clear()
        state = await w.client.get_catalogue_projection_state()
        assert state.availability == ("available" if cache == "pin" else "stale")
        assert len(w.checks) == 1
        assert (len(w.reads), len(await catalogue_effects(w))) == before


async def test_ttl_expiring_during_initial_fresh_check_keeps_original_second_check(catalogue):
    w = catalogue
    assert (await w.client.get_catalogue_projection_state()).availability == "available"
    # A sees a valid entry. The actual context wrapper advances time by 3s,
    # so the unchanged TTL branch must re-read instead of returning that entry.
    w.clock = w.client._catalogue_cache.expires_at - 1
    w.checks.clear()
    assert (await w.client.get_catalogue_projection_state()).availability == "available"
    # A and B both remain: only the private preparation C/D pair is merged.
    assert len(w.checks) == 5 and w.reads == ["one", "one"]
    assert len(await catalogue_effects(w)) == 2


async def test_certain_miss_source_revocation_closes_pin_before_catalogue_io(catalogue):
    w = catalogue
    with pytest.raises(CatalogueResolutionExpired):
        async with w.client.catalogue_resolution_scope(w.scope):
            pin = w.client._catalogue_resolution.get()
            async with get_db_session() as writer:
                (await writer.get(WorkspaceMember, (w.workspace, w.owner))).status = "removed"
            try:
                await w.client.get_catalogue_projection_state()
            finally:
                assert pin.closed
    assert w.reads == [] and await catalogue_effects(w) == []


async def test_certain_miss_cancel_during_source_check_closes_only_its_pin(catalogue, monkeypatch):
    w = catalogue
    entered = asyncio.Event()
    original = runtime_operation.runtime_context

    async def checking(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await asyncio.Event().wait()
        return result

    monkeypatch.setattr(runtime_operation, "runtime_context", checking)
    with pytest.raises(CatalogueResolutionExpired):
        async with w.client.catalogue_resolution_scope(w.scope):
            pin = w.client._catalogue_resolution.get()
            pending = asyncio.create_task(w.client.get_catalogue_projection_state())
            await asyncio.wait_for(entered.wait(), 5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert pin.closed
    assert w.reads == [] and await catalogue_effects(w) == []


async def test_certain_miss_cannot_release_late_bytes_after_its_pin_scope_exits(catalogue):
    w = catalogue
    entered, finish = asyncio.Event(), asyncio.Event()
    original = w.remote_handler

    async def delayed(request):
        result = await original(request)
        if request.url.path.endswith("/catalog"):
            entered.set()
            await finish.wait()
        return result

    w.remote_handler = delayed
    pending = None
    try:
        async with w.client.catalogue_resolution_scope(w.scope):
            pin = w.client._catalogue_resolution.get()
            pending = asyncio.create_task(w.client.get_catalogue_projection())
            await asyncio.wait_for(entered.wait(), 5)
        assert pin.closed
        finish.set()
        with pytest.raises(CatalogueResolutionExpired):
            await pending
        effect, = await catalogue_effects(w)
        # Already-completed transport remains settled, but this exited caller
        # cannot consume its bytes or borrow the scope for another operation.
        assert effect.state == "succeeded" and effect.attempt_count == 1
        assert w.reads == ["one"]
    finally:
        finish.set()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_private_refresh_unknown_retains_lkg_but_never_returns_or_resends_it(catalogue):
    w = catalogue
    assert (await w.client.get_catalogue_projection_state()).availability == "available"
    previous = w.client._catalogue_cache
    w.clock = previous.expires_at + 1
    original = w.remote_handler

    async def lost(request):
        result = await original(request)
        if request.url.path.endswith("/catalog"):
            raise httpx.ReadTimeout("local fixture lost the original response", request=request)
        return result

    w.remote_handler = lost
    assert (await w.client.get_catalogue_projection_state()).availability == "unavailable"
    assert w.client._catalogue_cache is previous
    effects = await catalogue_effects(w)
    assert [effect.state for effect in effects] == ["succeeded", "outcome_unknown"]
    w.remote_handler = original
    with pytest.raises(runtime_operation.RuntimePreparationUncertain):
        await w.client.get_catalogue_projection()
    assert w.reads == ["one", "one"] and len(await catalogue_effects(w)) == 2


async def test_private_control_close_after_transport_still_rejects_post_response(catalogue):
    from assistant import resource_control as controls
    from session.internal_parts import begin_session_write
    w = catalogue
    original = w.remote_handler

    async def closed(request):
        result = await original(request)
        if request.url.path.endswith("/catalog"):
            async with get_db_session() as writer:
                await begin_session_write(writer)
                await controls.close_admission_locked(writer, w.checks[-1][1], user_id=w.owner)
        return result

    w.remote_handler = closed
    assert (await w.client.get_catalogue_projection_state()).availability == "unavailable"
    effect, = await catalogue_effects(w)
    assert effect.state == "outcome_unknown" and effect.attempt_count == 1
    assert w.reads == ["one"]


async def change_catalogue_authority(w, change, fence, reader_pid):
    """Commit through a separate connection while the caller retains old rows."""
    from db.models.agent_driver import AgentDriverState
    from session.internal_parts import begin_session_write
    async with get_db_session() as writer:
        await begin_session_write(writer)
        writer_pid = await writer.scalar(text("select pg_backend_pid()")) if reader_pid else None
        if reader_pid:
            assert writer_pid != reader_pid
        if change == "membership":
            (await writer.get(WorkspaceMember, (w.workspace, w.owner))).status = "removed"
        elif change == "generation":
            (await writer.get(AgentDriverState, w.session.id)).generation += 1
        else:
            assert change == "resource"
            await runtime_operation.controls.close_admission_locked(writer, fence, user_id=w.owner)
    return writer_pid


@pytest.mark.parametrize("change", ["membership", "generation", "resource"])
async def test_private_framework_context_rejects_independent_change_after_catalogue_precheck(
        catalogue, monkeypatch, record_property, change):
    from agent.driver import LeaseLostError
    from assistant.policy import AssistantError
    w = catalogue
    context = await runtime_operation.runtime_context(w.client, w.lease)
    w.checks.clear()
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime_operation.runtime_context

    async def after_b(*args, **kwargs):
        result = await original(*args, **kwargs)
        if not entered.is_set():
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(runtime_operation, "runtime_context", after_b)
    async with get_db_session() as held:
        old_member = await held.get(WorkspaceMember, (w.workspace, w.owner))
        reader_pid = await held.scalar(text("select pg_backend_pid()")) if get_engine().dialect.name == "postgresql" else None
        pending = asyncio.create_task(w.client.get_catalogue_projection())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            writer_pid = await change_catalogue_authority(w, change, context[1], reader_pid)
            record_property("independent_writer", json.dumps({"reader_pid": reader_pid, "writer_pid": writer_pid}))
            assert old_member.status == "active"
            release.set()
            with pytest.raises(LeaseLostError if change == "generation" else AssistantError) as refused:
                await pending
            if change != "generation":
                assert refused.value.code == ("ASSISTANT_WORKSPACE_FORBIDDEN" if change == "membership"
                                              else "RESOURCE_CONTROL_HELD")
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    assert len(w.checks) == 1 and w.reads == [] and await catalogue_effects(w) == []


async def test_private_http_gate_rejects_independent_close_after_original_claim(
        catalogue, monkeypatch, record_property):
    from assistant.policy import AssistantError
    w = catalogue
    context = await runtime_operation.runtime_context(w.client, w.lease)
    w.checks.clear()
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime_operation.effects.claim_effect_for_dispatch

    async def claimed(*args, **kwargs):
        claim = await original(*args, **kwargs)
        assert claim is not None
        entered.set()
        await release.wait()
        return claim

    monkeypatch.setattr(runtime_operation.effects, "claim_effect_for_dispatch", claimed)
    async with get_db_session() as held:
        await held.get(WorkspaceMember, (w.workspace, w.owner))
        reader_pid = await held.scalar(text("select pg_backend_pid()")) if get_engine().dialect.name == "postgresql" else None
        pending = asyncio.create_task(w.client.get_catalogue_projection())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            writer_pid = await change_catalogue_authority(w, "resource", context[1], reader_pid)
            record_property("independent_writer", json.dumps({"reader_pid": reader_pid, "writer_pid": writer_pid}))
            release.set()
            with pytest.raises(AssistantError) as refused:
                await pending
            assert refused.value.code == "RESOURCE_CONTROL_HELD"
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    effect, = await catalogue_effects(w)
    assert effect.state == "prepared" and effect.attempt_count == 0
    assert effect.claim_token is None and effect.submitting_at is None
    assert len(w.checks) == 2 and w.reads == []


async def test_cancelled_private_framework_context_cannot_prepare_or_escape_pin(catalogue, monkeypatch):
    w = catalogue
    await runtime_operation.runtime_context(w.client, w.lease)
    w.checks.clear()
    entered = asyncio.Event()
    original = runtime_operation.runtime_context

    async def framework_context(*args, **kwargs):
        result = await original(*args, **kwargs)
        if len(w.checks) == 2:  # B completed; the real mandatory D now completed.
            entered.set()
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(runtime_operation, "runtime_context", framework_context)
    async with w.client.catalogue_resolution_scope(w.scope):
        pin = w.client._catalogue_resolution.get()
        pending = asyncio.create_task(w.client.get_catalogue_projection())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert pin.state is None
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    assert pin.closed and w.client._catalogue_resolution.get() is None
    assert w.reads == [] and await catalogue_effects(w) == []


async def test_public_runtime_callback_keeps_initial_http_and_completed_guards(catalogue):
    w = catalogue
    await runtime_operation.runtime_context(w.client, w.lease)
    w.checks.clear()
    guarded = []

    async def current():
        await runtime_operation.runtime_context(w.client, w.lease)
        guarded.append(len(w.checks))

    async def read():
        assert (await w.client._reload_catalogue_projection()).availability == "available"
        return {"observed": True}

    result = await runtime_operation.run_runtime_operation(w.client,
        session_id=w.session.id, user_id=w.owner, stage="catalogue_read",
        key="public-callback-contract", payload={"surface": "fixture_catalogue"},
        operation=read, before_request=current)
    # Public callers retain C + mandatory D + E + F, even on a private client.
    assert result == {"observed": True} and guarded == [1, 3, 4]
    assert len(w.checks) == 4 and w.reads == ["one"]
    effect, = await catalogue_effects(w)
    assert effect.state == "succeeded" and effect.attempt_count == 1
