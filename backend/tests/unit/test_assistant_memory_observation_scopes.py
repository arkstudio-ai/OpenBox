"""An observation may reuse only its own freshly resolved exact memory scope.

Real SQL and provenance validation are compared with the prior unseeded path.
Only the existing embedding/index HTTP fixture is substituted during capture.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from assistant import knowledge, knowledge_provenance, memory, memory_documents, memory_provenance
from assistant.policy import AssistantError
from assistant.transactions import begin_snapshot
from db.base import get_db_session, get_engine
from db.models.memory_v2 import MemorySource
from db.models.project import Project
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_knowledge import page_for
from tests.unit.test_assistant_memory_reads import external_io, no_dispatch, note, seed  # noqa: F401


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from tests.offline_wuying import install_wuying_offline_guard
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    install_wuying_offline_guard(monkeypatch)


@contextmanager
def statements():
    rows = []

    def collect(_connection, _cursor, statement, _parameters, *_rest):
        rows.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", collect)
    try:
        yield rows
    finally:
        event.remove(engine, "before_cursor_execute", collect)


async def observation(monkeypatch, operation, selection="project"):
    identity, _, projects, _ = await seed(monkeypatch)
    selected = projects[0]
    project_ids = {"project": [selected], "background": [None],
                   "mixed": [None, selected], "all": [None, selected, projects[1]]}[selection]
    scope = ({"include_all_projects": True} if selection == "all" else
             {} if selection == "background" else {"project_id": selected})
    for index, project_id in enumerate(project_ids):
        if operation.startswith("memory."):
            await note(identity, f"MEMORY_CANARY observation scope {index}", project_id)
        else:
            await page_for(identity, project_id, f"MEMORY_CANARY observation scope {index}")
    async with get_db_session() as db:
        main = await db.get(Session, identity["main_id"])
    provenance = memory_provenance if operation.startswith("memory.") else knowledge_provenance
    arguments = {**scope, "query": "MEMORY_CANARY", "limit": 20}
    if operation.endswith("read"):
        listing = (await memory.search(**identity, **arguments) if operation.startswith("memory.") else
                   await knowledge.directory(**identity, **arguments))
        project_id = None if selection == "background" else selected
        reference = next(item["source_ref"] for item in listing["items"] if item["project_id"] == project_id)
        arguments = {**scope, "source_ref": reference, "max_chars": 1000}
    _, snapshot = await provenance.capture(main, arguments, operation=operation)
    return SimpleNamespace(main=main, provenance=provenance, snapshot=snapshot,
                           identity=identity, project_id=selected, project_ids=project_ids)


async def measured(monkeypatch, world, *, fresh, seeded):
    views, scopes = [], []
    original_snapshot = deepcopy(world.snapshot)

    def projection(module, name, original):
        async def observe(*args, **kwargs):
            if not seeded:
                # Invoke the original facade's default behavior, while keeping
                # all real source, body, identity and proof checks in place.
                kwargs.pop("local_scopes", None)
            result = await original(*args, **kwargs)
            views.append((module.__name__, name, deepcopy(result)))
            return result
        return observe

    def resolution(original):
        async def observe(*args, **kwargs):
            scopes.append((kwargs.get("project_id"), kwargs.get("include_all_projects", False)))
            return await original(*args, **kwargs)
        return observe

    error = None
    with monkeypatch.context() as patch:
        for module, names in ((knowledge, ("_revalidate_refs_locked", "_directory_locked", "_read_locked")),
                              (memory, ("revalidate_items", "_read_locked"))):
            for name in names:
                patch.setattr(module, name, projection(module, name, getattr(module, name)))
        for module in (knowledge, memory, memory_documents):
            patch.setattr(module, "resolve_access_scope", resolution(module.resolve_access_scope))
        with statements() as sql:
            try:
                assert await world.provenance.validate(world.main, world.snapshot, fresh=fresh) is None
            except AssistantError as exc:
                error = (exc.status, exc.code)
    assert world.snapshot == original_snapshot
    assert all(statement.lstrip().upper().startswith(("SELECT", "BEGIN", "SET TRANSACTION")) for statement in sql)
    return SimpleNamespace(views=views, scopes=scopes, error=error,
                           selects=sum(statement.lstrip().upper().startswith("SELECT") for statement in sql))


@pytest.mark.parametrize("operation", ["memory.search", "memory.read", "knowledge.directory", "knowledge.read"])
@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("selection", ["project", "background", "all"])
async def test_exact_scope_reuse_preserves_all_projection_bytes_and_read_only_checks(
        monkeypatch, external_io, record_property, operation, fresh, selection):
    world = await observation(monkeypatch, operation, selection)
    external_count = len(external_io.calls)
    baseline = await measured(monkeypatch, world, fresh=fresh, seeded=False)
    optimized = await measured(monkeypatch, world, fresh=fresh, seeded=True)
    assert baseline.error is optimized.error is None
    assert optimized.views == baseline.views
    assert len(external_io.calls) == external_count
    stages = 2 if operation == "knowledge.directory" and fresh else 1
    saving = 0 if selection == "all" else stages * (2 if selection == "background" else 3)
    assert baseline.selects - optimized.selects == saving
    if selection == "all":
        assert optimized.scopes == baseline.scopes
    else:
        selected = None if selection == "background" else world.project_id
        assert baseline.scopes == [(selected, False)] * (stages + 1)
        assert optimized.scopes == [(selected, False)]
    record_property("scope_sql", json.dumps({"operation": operation, "fresh": fresh,
        "selection": selection, "baseline": baseline.selects, "optimized": optimized.selects,
        "saved": saving, "projection_bytes_equal": True, "network_during_validation": 0}))


@pytest.mark.parametrize("operation", ["memory.search", "knowledge.directory"])
async def test_explicit_project_still_revalidates_background_separately(
        monkeypatch, external_io, operation):
    world = await observation(monkeypatch, operation, "mixed")
    baseline = await measured(monkeypatch, world, fresh=True, seeded=False)
    optimized = await measured(monkeypatch, world, fresh=True, seeded=True)
    assert baseline.error is optimized.error is None
    assert optimized.views == baseline.views
    stages = 2 if operation == "knowledge.directory" else 1
    assert baseline.selects - optimized.selects == 3 * stages
    assert optimized.scopes.count((None, False)) == stages
    assert optimized.scopes.count((world.project_id, False)) == 1
    items = optimized.views[0][2]
    assert {item["project_id"] for item in items} == {None, world.project_id}


@pytest.mark.parametrize("operation", ["memory.read", "knowledge.read"])
@pytest.mark.parametrize("change", ["membership", "project"])
async def test_next_observation_rejects_independent_revocation_despite_outer_held_snapshot(
        monkeypatch, external_io, operation, change):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent writer while an old RR is held requires PostgreSQL")
    world = await observation(monkeypatch, operation)
    async with get_db_session() as held:
        await begin_snapshot(held)
        world.main = await held.get(Session, world.main.id)
        project = await held.get(Project, world.project_id)
        member = await held.scalar(select(WorkspaceMember).where(
            WorkspaceMember.user_id == world.main.user_id, WorkspaceMember.workspace_id == world.main.workspace_id))
        assert not project.is_deleted and member.status == "active"
        assert (await measured(monkeypatch, world, fresh=False, seeded=True)).error is None
        async with get_db_session() as writer:
            if change == "project":
                (await writer.get(Project, world.project_id)).is_deleted = True
            else:
                row = await writer.scalar(select(WorkspaceMember).where(
                    WorkspaceMember.user_id == world.main.user_id, WorkspaceMember.workspace_id == world.main.workspace_id))
                row.status = "revoked"
        # The old outer SQL snapshot remains available. It cannot certify the
        # new observation, which must establish its own scope in a fresh RR.
        assert await held.scalar(select(Project.is_deleted).where(Project.id == world.project_id)) is False
        assert await held.scalar(select(WorkspaceMember.status).where(
            WorkspaceMember.user_id == member.user_id,
            WorkspaceMember.workspace_id == member.workspace_id)) == "active"
        for fresh in (False, True):
            baseline = await measured(monkeypatch, world, fresh=fresh, seeded=False)
            optimized = await measured(monkeypatch, world, fresh=fresh, seeded=True)
            assert optimized.error == baseline.error and optimized.error is not None
            assert optimized.views == baseline.views == []


@pytest.mark.parametrize("operation", ["memory.search", "knowledge.directory"])
async def test_source_refusal_precedes_later_observation_proof_damage(monkeypatch, external_io, operation):
    world = await observation(monkeypatch, operation)
    item = world.snapshot["projection"]["items"][0]
    if operation == "memory.search":
        source_id = item["sources"][0]["id"]
    else:
        from db.models.memory_wiki import MemoryWikiPage
        async with get_db_session() as db:
            source_id = (await db.get(MemoryWikiPage, item["id"])).source_manifest[0]["id"]
    async with get_db_session() as db:
        (await db.get(MemorySource, source_id)).status = "REVOKED"
    world.snapshot["sources"]["resources"][0]["damaged_proof"] = True
    for fresh in (False, True):
        baseline = await measured(monkeypatch, world, fresh=fresh, seeded=False)
        optimized = await measured(monkeypatch, world, fresh=fresh, seeded=True)
        expected = "ASSISTANT_MEMORY_UNAVAILABLE" if operation == "memory.search" else "ASSISTANT_KNOWLEDGE_UNAVAILABLE"
        assert optimized.error == baseline.error == (410, expected)
        assert optimized.views == baseline.views == []
