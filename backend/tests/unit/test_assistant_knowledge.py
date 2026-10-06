"""Stage 4 directory preparation: exact scope and fresh, body-free evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import event

from assistant import knowledge
from assistant.policy import AssistantError
from assistant.service import ensure_main_session
from core.config import OpenBoxConfig
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemorySource, MemoryTombstone
from db.models.memory_wiki import MemoryWikiPage
from db.models.project import Project
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from memory import service as memory_service
from memory.policy import MemoryAccessDenied, resolve_access_scope
from memory.redaction import text_hash
from memory.session_policy import require_context_memory
from memory.wiki import service
from session.session import create_session
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tool.tool import ToolContext


async def seed(monkeypatch):
    owner, other, workspace = await accounts()
    config = OpenBoxConfig(jwt_secret="test-assistant-knowledge-cursors",
        memory={"wiki": True, "v2_write": True, "automatic_knowledge": False, "allowed_user_ids": [owner, other]})
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr("assistant.knowledge.get_config", lambda: config)
    monkeypatch.setattr("assistant.history.get_config", lambda: config)
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    now = datetime.now(timezone.utc)
    projects = []
    async with get_db_session() as db:
        for name, user_id in (("A", owner), ("B", owner), ("Other", other)):
            project = Project(id=uuid4().hex, user_id=user_id, workspace_id=workspace, name=name,
                              created_at=now, updated_at=now)
            db.add(project)
            projects.append(project.id)
    return {"user_id": owner, "workspace_id": workspace, "main_id": main.id}, other, projects, config


async def page_for(identity, project_id, title, *, page_id=None, note=None):
    note = note or await memory_service.create_note(user_id=identity["user_id"], workspace_id=identity["workspace_id"],
        project_id=project_id, summary="PRIVATE_BODY_" + title)
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=identity["user_id"], workspace_id=identity["workspace_id"],
                                          project_id=project_id)
        sources, memories = await service.collect_compile_sources(db, scope, [note["id"]])
        page = MemoryWikiPage(id=page_id or uuid4().hex, target_identity=uuid4().hex,
            user_id=identity["user_id"], workspace_id=identity["workspace_id"], project_id=project_id,
            visibility="PERSONAL", slug=uuid4().hex, title=title, revision=1, body="PRIVATE_BODY_" + title,
            content_hash=text_hash("PRIVATE_BODY_" + title), status="PUBLISHED",
            source_manifest=[service._source_ref(source, scope) for source in sources.values()],
            memory_manifest=memories, paragraphs=[], acl_epoch=scope.acl_epoch, policy_version="test-v1",
            model="test/no-provider", input_hash=uuid4().hex, candidate_id=uuid4().hex,
            created_at=now, updated_at=now)
        db.add(page)
    return page, note


async def test_personal_default_explicit_all_projects_and_foreign_scopes(monkeypatch):
    identity, other, projects, _ = await seed(monkeypatch)
    personal, _ = await page_for(identity, None, "Personal")
    a, _ = await page_for(identity, projects[0], "Project A")
    b, _ = await page_for(identity, projects[1], "Project B")
    foreign, _ = await page_for({**identity, "user_id": other}, projects[2], "OTHER_OWNER_CANARY")
    other_identity, _, _, _ = await seed(monkeypatch)
    other_workspace, _ = await page_for(other_identity, None, "OTHER_WORKSPACE_CANARY")
    # Restore the original feature allowlist after constructing the other scope.
    monkeypatch.setattr("assistant.knowledge.get_config", lambda: OpenBoxConfig(
        jwt_secret="test-assistant-knowledge-cursors", memory={"wiki": True, "allowed_user_ids": [identity["user_id"]]}))
    result = await knowledge.directory(**identity)
    assert [item["id"] for item in result["items"]] == [personal.id]
    assert result["scope"]["include_all_projects"] is False
    selected = await knowledge.directory(**identity, project_id=projects[0])
    assert {item["id"] for item in selected["items"]} == {personal.id, a.id}
    every = await knowledge.directory(**identity, include_all_projects=True)
    assert {item["id"] for item in every["items"]} == {personal.id, a.id, b.id}
    assert all(value not in json.dumps(every) for value in (
        foreign.id, other_workspace.id, "OTHER_OWNER_CANARY", "OTHER_WORKSPACE_CANARY", "PRIVATE_BODY_"))
    with pytest.raises(AssistantError):
        await knowledge.directory(**identity, project_id=projects[2])
    with pytest.raises(AssistantError):
        await knowledge.directory(**{**identity, "user_id": other}, include_all_projects=True)
    with pytest.raises(ValueError):
        await knowledge.directory(**identity, project_id=projects[0], include_all_projects=True)


async def test_all_project_discovery_does_not_broaden_each_pages_source_domain(monkeypatch):
    identity, _, projects, _ = await seed(monkeypatch)
    a, _ = await page_for(identity, projects[0], "A")
    b, _ = await page_for(identity, projects[1], "CROSS_PROJECT_CANARY")
    async with get_db_session() as db:
        row = await db.get(MemoryWikiPage, a.id)
        row.source_manifest, row.memory_manifest = b.source_manifest, b.memory_manifest
        row.title = "BORROWED_SOURCE_CANARY"
    result = await knowledge.directory(**identity, include_all_projects=True)
    assert [item["id"] for item in result["items"]] == [b.id]
    assert "BORROWED_SOURCE_CANARY" not in json.dumps(result)


@pytest.mark.parametrize("change", ["source_tombstone", "memory_tombstone", "page_tombstone", "source_body",
                                    "memory_revision", "source_session_deleted", "page_body", "project_deleted"])
async def test_revoked_sources_hide_the_entire_item_and_invalidate_old_refs(monkeypatch, change):
    identity, _, projects, _ = await seed(monkeypatch)
    page, note = await page_for(identity, projects[0], "REVOKED_TITLE_CANARY")
    original = await create_session(user_id=identity["user_id"], workspace_id=identity["workspace_id"],
                                    project_id=projects[0])
    source_id = page.source_manifest[0]["id"]
    async with get_db_session() as db:
        (await db.get(MemorySource, source_id)).session_id = original.id
    captured = await knowledge.directory(**identity, include_all_projects=True)
    ref = captured["items"][0]["source_ref"]
    assert (await knowledge.read(**identity, source_ref=ref, include_all_projects=True))["item"] == captured["items"][0]
    async with get_db_session() as db:
        if change.endswith("tombstone"):
            kind, object_id = {"source_tombstone": ("source", source_id), "memory_tombstone": ("memory", note["id"]),
                               "page_tombstone": ("wiki", page.id)}[change]
            db.add(MemoryTombstone(id=uuid4().hex, user_id=identity["user_id"], workspace_id=identity["workspace_id"],
                project_id=projects[0], object_kind=kind, object_id=object_id, revision=1,
                content_hash=page.content_hash, deleted_at=datetime.now(timezone.utc)))
        elif change == "source_body":
            (await db.get(MemorySource, source_id)).body = "REPLACED_WITHOUT_REVISION"
        elif change == "memory_revision":
            (await db.get(UserMemory, note["id"])).revision += 1
        elif change == "source_session_deleted":
            (await db.get(Session, original.id)).is_deleted = True
        elif change == "page_body":
            (await db.get(MemoryWikiPage, page.id)).body = "REPLACED_WITHOUT_REVISION"
        elif change == "project_deleted":
            (await db.get(Project, projects[0])).is_deleted = True
    result = await knowledge.directory(**identity, include_all_projects=True, query="REVOKED_TITLE_CANARY")
    assert result["items"] == []
    assert "REVOKED_TITLE_CANARY" not in json.dumps(result)
    with pytest.raises(AssistantError, match="unavailable"):
        await knowledge.read(**identity, source_ref=ref, include_all_projects=True)


@pytest.mark.parametrize("change", ["title", "source_identity", "acl_epoch", "reference", "policy_disabled"])
async def test_metadata_versions_and_current_authority_are_rechecked(monkeypatch, change):
    identity, _, projects, config = await seed(monkeypatch)
    page, _ = await page_for(identity, projects[0], "Original title")
    result = await knowledge.directory(**identity, include_all_projects=True)
    refs = deepcopy([result["items"][0]["source_ref"]])
    async with get_db_session() as db:
        if change == "title":
            (await db.get(MemoryWikiPage, page.id)).title = "Changed title"
        elif change == "source_identity":
            (await db.get(MemorySource, page.source_manifest[0]["id"])).source_metadata = {"changed": True}
        elif change == "acl_epoch":
            member = await db.get(WorkspaceMember, (identity["workspace_id"], identity["user_id"]))
            member.updated_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        elif change == "reference":
            refs[0]["metadata_hash"] = "0" * 64
        elif change == "policy_disabled":
            config.memory.wiki = False
    with pytest.raises(AssistantError):
        await knowledge.read(**identity, source_ref=refs[0], include_all_projects=True)


async def test_paging_rechecks_every_source_and_binds_scope_filter_limit_version_and_expiry(monkeypatch):
    identity, other, projects, _ = await seed(monkeypatch)
    first, _ = await page_for(identity, projects[0], "Match first", page_id="a" + uuid4().hex)
    second, _ = await page_for(identity, projects[0], "Match second", page_id="b" + uuid4().hex)
    third, _ = await page_for(identity, projects[0], "Match third", page_id="c" + uuid4().hex)
    initial = await knowledge.directory(**identity, include_all_projects=True, query="Match", limit=1)
    assert [item["id"] for item in initial["items"]] == [first.id]
    cursor = initial["next_cursor"]
    assert cursor and first.id not in cursor
    for changed in ({"query": "other"}, {"limit": 2}, {"include_all_projects": False}, {"user_id": other}):
        args = {**identity, "include_all_projects": True, "query": "Match", "limit": 1, "cursor": cursor, **changed}
        with pytest.raises(AssistantError):
            await knowledge.directory(**args)
    async with get_db_session() as db:
        (await db.get(MemorySource, second.source_manifest[0]["id"])).deleted_at = datetime.now(timezone.utc)
    next_page = await knowledge.directory(**identity, include_all_projects=True, query="Match", limit=1, cursor=cursor)
    assert [item["id"] for item in next_page["items"]] == [third.id]
    assert second.id not in json.dumps(next_page) and "Match second" not in json.dumps(next_page)
    monkeypatch.setattr(knowledge, "VERSION", 2)
    with pytest.raises(AssistantError):
        await knowledge.directory(**identity, include_all_projects=True, query="Match", limit=1, cursor=cursor)
    monkeypatch.setattr(knowledge, "VERSION", 1)
    instant = knowledge.time.time()
    monkeypatch.setattr(knowledge.time, "time", lambda: instant + knowledge.CURSOR_TTL + 1)
    with pytest.raises(AssistantError):
        await knowledge.directory(**identity, include_all_projects=True, query="Match", limit=1, cursor=cursor)


async def test_scan_is_bounded_and_unavailable_ids_stay_inside_encrypted_cursor(monkeypatch):
    identity, _, projects, _ = await seed(monkeypatch)
    bad, _ = await page_for(identity, projects[0], "STALE_CANARY", page_id="a" + uuid4().hex)
    good, _ = await page_for(identity, projects[0], "Visible", page_id="b" + uuid4().hex)
    async with get_db_session() as db:
        (await db.get(MemorySource, bad.source_manifest[0]["id"])).body = "tampered"
    monkeypatch.setattr(knowledge, "MAX_SCAN", 1)
    first = await knowledge.directory(**identity, include_all_projects=True)
    assert first["items"] == [] and first["next_cursor"]
    assert bad.id not in json.dumps(first) and "STALE_CANARY" not in json.dumps(first)
    second = await knowledge.directory(**identity, include_all_projects=True, cursor=first["next_cursor"])
    assert [item["id"] for item in second["items"]] == [good.id]


async def test_reads_are_sql_only_and_memory_tools_remain_isolated(monkeypatch):
    from db import base
    identity, _, projects, _ = await seed(monkeypatch)
    await page_for(identity, projects[0], "Visible")
    statements = []

    def checked(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().lower())

    engine = base._engine.sync_engine
    event.listen(engine, "before_cursor_execute", checked)
    try:
        await knowledge.directory(**identity, include_all_projects=True)
    finally:
        event.remove(engine, "before_cursor_execute", checked)
    assert statements and all(sql.startswith(("select", "begin", "set transaction")) for sql in statements)
    with pytest.raises(MemoryAccessDenied):
        await require_context_memory(ToolContext(session_id=identity["main_id"], user_id=identity["user_id"],
                                                  workspace_id=identity["workspace_id"]))
    async with get_db_session() as db:
        assert (await db.get(Session, identity["main_id"])).memory_policy == "assistant_isolated"


async def test_revoked_membership_blocks_even_empty_reads(monkeypatch):
    identity, _, _, _ = await seed(monkeypatch)
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (identity["workspace_id"], identity["user_id"]))).status = "removed"
    with pytest.raises(AssistantError):
        await knowledge.directory(**identity)


async def test_first_item_and_whole_page_respect_serialized_byte_budget(monkeypatch):
    identity, _, projects, _ = await seed(monkeypatch)
    for i in range(6):
        await page_for(identity, projects[0], f"{i}:" + "目" * 158)
    monkeypatch.setattr(knowledge, "MAX_RESPONSE_BYTES", 4300)
    with pytest.raises(AssistantError) as oversized:
        await knowledge.directory(**identity, include_all_projects=True)
    assert oversized.value.code == "ASSISTANT_CONTEXT_BUDGET"
    monkeypatch.setattr(knowledge, "MAX_RESPONSE_BYTES", 7000)
    result = await knowledge.directory(**identity, include_all_projects=True)
    assert 1 <= len(result["items"]) < 6 and result["next_cursor"]
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= knowledge.MAX_RESPONSE_BYTES
    next_page = await knowledge.directory(**identity, include_all_projects=True, cursor=result["next_cursor"])
    assert not ({item["id"] for item in result["items"]} & {item["id"] for item in next_page["items"]})
    assert len(json.dumps(next_page, ensure_ascii=False).encode()) <= knowledge.MAX_RESPONSE_BYTES


@pytest.mark.parametrize("bad", [{"extra": "x" * 70000}, {"metadata_hash": "x" * 70000},
                                  {"revision": True}, {"revision": 2 ** 63}, {"project_id": []},
                                  {"acl_epoch": -1}, {"id": ""}])
async def test_reference_structure_rejects_oversized_or_malformed_values(monkeypatch, bad):
    identity, _, projects, _ = await seed(monkeypatch)
    await page_for(identity, projects[0], "Visible")
    result = await knowledge.directory(**identity, include_all_projects=True)
    ref = {**result["items"][0]["source_ref"], **bad}
    with pytest.raises(AssistantError):
        await knowledge.read(**identity, source_ref=ref, include_all_projects=True)


async def test_postgres_old_read_snapshot_cannot_reauthorize_a_later_read(monkeypatch):
    from db import base
    if base._engine.dialect.name != "postgresql":
        pytest.skip("Independent PostgreSQL writer and read-only snapshot")
    identity, _, projects, _ = await seed(monkeypatch)
    page, _ = await page_for(identity, projects[0], "Revoked after capture")
    checked, resume = asyncio.Event(), asyncio.Event()
    original = knowledge._current_item

    async def paused(*args):
        result = await original(*args)
        checked.set()
        await asyncio.wait_for(resume.wait(), 10)
        return result

    monkeypatch.setattr(knowledge, "_current_item", paused)
    pending = asyncio.create_task(knowledge.directory(**identity, include_all_projects=True))
    try:
        await asyncio.wait_for(checked.wait(), 10)
        async with get_db_session() as db:
            (await db.get(MemorySource, page.source_manifest[0]["id"])).deleted_at = datetime.now(timezone.utc)
    finally:
        resume.set()
    captured = await asyncio.wait_for(pending, 10)
    assert captured["items"][0]["id"] == page.id
    monkeypatch.setattr(knowledge, "_current_item", original)
    with pytest.raises(AssistantError):
        await knowledge.read(**identity, source_ref=captured["items"][0]["source_ref"], include_all_projects=True)
    assert (await knowledge.directory(**identity, include_all_projects=True))["items"] == []


@pytest.mark.parametrize("change", ["status", "revision", "sections"])
async def test_uploaded_document_dependencies_recheck_the_original_sql_revision(monkeypatch, change):
    from db.models.memory_document import MemoryDocument, MemoryDocumentRevision
    from sqlalchemy import select
    from tests.unit.test_memory_documents import ingest

    identity, _, projects, config = await seed(monkeypatch)
    data = (identity["user_id"], identity["workspace_id"], projects[0], None, config.memory)
    document, _ = await ingest(data, "# Document title\n\nPrivate source sentence.", filename="Internal test.txt")
    result = await knowledge.directory(**identity, include_all_projects=True)
    assert {item["id"] for item in result["items"]} == set(document.page_ids)
    refs = [item["source_ref"] for item in result["items"]]
    async with get_db_session() as db:
        row = await db.get(MemoryDocument, document.id)
        if change == "status":
            row.status = "DELETED"
        elif change == "revision":
            row.revision += 1
        else:
            revision = await db.scalar(select(MemoryDocumentRevision).where(
                MemoryDocumentRevision.document_id == row.id, MemoryDocumentRevision.revision == row.revision))
            revision.sections = [{"title": "Replaced", "body": "Unversioned new body"}]
    assert (await knowledge.directory(**identity, include_all_projects=True))["items"] == []
    for ref in refs:
        with pytest.raises(AssistantError):
            await knowledge.read(**identity, source_ref=ref, include_all_projects=True)


async def corrected_page(monkeypatch):
    from core import config as runtime_config
    from memory.extraction import MemoryExtractionWorker
    from tests.unit.test_memory_reconciliation import Planner, Verifier, proposal, seed_correction

    data, note, _ = await seed_correction(monkeypatch)
    assert await MemoryExtractionWorker(extractor=proposal, verifier=Verifier(),
        reconciler=Planner(note["id"])).run_once() == "SUCCEEDED"
    config = runtime_config.get_config()
    config.jwt_secret = "test-assistant-knowledge-cursors"
    monkeypatch.setattr("assistant.knowledge.get_config", lambda: config)
    monkeypatch.setattr("assistant.history.get_config", lambda: config)
    main = await ensure_main_session(user_id=data[0], workspace_id=data[1])
    identity = {"user_id": data[0], "workspace_id": data[1], "main_id": main.id}
    page, _ = await page_for(identity, data[2], "Verified correction", note=note)
    async with get_db_session() as db:
        source = await db.get(MemorySource, page.source_manifest[0]["id"])
        assert source.source_kind == "verified_memory_revision"
        leaf_id = source.source_metadata["change_source_ids"][0]
        assert (await db.get(MemorySource, leaf_id)).session_id == data[3]
    return identity, page, leaf_id


@pytest.mark.parametrize("change", ["metadata", "session_rebinding", "branch_span", "occurred_at"])
async def test_verified_revision_leaf_identity_changes_invalidate_frozen_directory_ref(monkeypatch, change):
    from db.models.message import Message
    from db.models.part import Part
    from memory.reconciliation import revision_sources_available

    identity, page, leaf_id = await corrected_page(monkeypatch)
    before = await knowledge.directory(**identity, include_all_projects=True)
    assert len(before["items"]) == 1
    ref = before["items"][0]["source_ref"]
    destination = await create_session(user_id=identity["user_id"], workspace_id=identity["workspace_id"],
                                       project_id=page.project_id)
    async with get_db_session() as db:
        leaf = await db.get(MemorySource, leaf_id)
        leaf_revision, leaf_hash = leaf.source_revision, leaf.content_hash
        if change == "metadata":
            leaf.source_metadata = {**leaf.source_metadata, "authority_binding": "changed-without-revision"}
        elif change == "session_rebinding":
            original = await db.get(Part, leaf.part_id)
            now = datetime.now(timezone.utc)
            message_id, part_id = uuid4().hex, uuid4().hex
            db.add(Message(id=message_id, user_id=identity["user_id"], session_id=destination.id,
                           role="user", created_at=now))
            await db.flush()
            db.add(Part(id=part_id, user_id=identity["user_id"], session_id=destination.id,
                message_id=message_id, type=original.type, data=deepcopy(original.data), created_at=now))
            leaf.session_id, leaf.message_id, leaf.part_id = destination.id, message_id, part_id
        elif change == "branch_span":
            leaf.branch_id, leaf.turn_id, leaf.start_seq, leaf.end_seq = "new-branch", "new-turn", 901, 902
        else:
            leaf.occurred_at = datetime.now(timezone.utc) + timedelta(days=1)
        await db.flush()
        direct = await db.get(MemorySource, page.source_manifest[0]["id"])
        scope = await resolve_access_scope(db, user_id=identity["user_id"], workspace_id=identity["workspace_id"],
                                          project_id=page.project_id)
        # The existing source authority still admits these valid same-body
        # sources. The frozen directory proof must additionally detect identity
        # rebinding, without pretending that id/revision/hash alone caught it.
        assert await revision_sources_available(db, scope, direct)
        assert (leaf.source_revision, leaf.content_hash) == (leaf_revision, leaf_hash)
    with pytest.raises(AssistantError):
        await knowledge.read(**identity, source_ref=ref, include_all_projects=True)
    after = await knowledge.directory(**identity, include_all_projects=True)
    assert after["items"][0]["source_ref"]["dependencies_hash"] != ref["dependencies_hash"]


@pytest.mark.parametrize("change", ["leaf_tombstone", "other_project", "recursive", "leaf_budget"])
async def test_verified_revision_leaf_closure_is_scoped_current_and_bounded(monkeypatch, change):
    from sqlalchemy import select

    identity, page, leaf_id = await corrected_page(monkeypatch)
    captured = await knowledge.directory(**identity, include_all_projects=True)
    assert len(captured["items"]) == 1
    ref = captured["items"][0]["source_ref"]
    if change == "leaf_budget":
        # A production reconciliation flattened the old note and correction
        # into two distinct leaves. Neither is lost when there is room; an
        # insufficient budget rejects the page rather than dropping evidence.
        monkeypatch.setattr(knowledge, "MAX_LEAF_SOURCES", 2)
        assert (await knowledge.read(**identity, source_ref=ref, include_all_projects=True))["text"]
        monkeypatch.setattr(knowledge, "MAX_LEAF_SOURCES", 1)
    else:
        async with get_db_session() as db:
            direct = await db.get(MemorySource, page.source_manifest[0]["id"])
            if change == "leaf_tombstone":
                db.add(MemoryTombstone(id=uuid4().hex, user_id=identity["user_id"],
                    workspace_id=identity["workspace_id"], project_id=page.project_id,
                    object_kind="source", object_id=leaf_id, revision=1, deleted_at=datetime.now(timezone.utc)))
            elif change == "recursive":
                direct.source_metadata = {**direct.source_metadata, "dependencies": [{"id": direct.id,
                    "revision": direct.source_revision, "content_hash": direct.content_hash}]}
            else:
                leaf = await db.get(MemorySource, leaf_id)
                # Transfer the original leaf together with its genuine chat
                # to another currently owned project. An all-project search
                # must still verify this page using its original domain.
                project = await db.scalar(select(Project).where(Project.user_id == identity["user_id"],
                    Project.workspace_id == identity["workspace_id"], Project.id != page.project_id))
                assert project is not None
                leaf.project_id = project.id
                (await db.get(Session, leaf.session_id)).project_id = project.id
    assert (await knowledge.directory(**identity, include_all_projects=True))["items"] == []
    with pytest.raises(AssistantError):
        await knowledge.read(**identity, source_ref=ref, include_all_projects=True)
