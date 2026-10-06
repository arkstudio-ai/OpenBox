"""Actual memory service/retrieval/SQL with isolated external HTTP and model IO.

No live provider/index/QA is contacted. Authorization, retrieval ranking, source
checks, tools, canonical processor checkpoints and final receipts are real.
V2 (PERSONAL_ASSISTANT_DESIGN_V2.md 8.4, D1): each memory.search/memory.read
checks the reader's current scope and sources; an observation is used by the
run that read it, later requests do not replay it, and saved answers are not
re-validated after a memory is forgotten.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, event, select

from assistant import memory
from assistant.memory_provenance import ReadArgs
from assistant.policy import AssistantError
from assistant.service import ensure_main_session
from db.base import get_db_session, get_engine
from db.models.agent_inbox import AgentInboxItem
from db.models.memory import UserMemory
from db.models.memory_v2 import MemoryRevision, MemorySource, MemorySourceLink
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from memory import retrieval, service
from memory.index.embedding import BailianEmbedding
from memory.index.qdrant import QdrantMemoryIndex
from memory.policy import resolve_access_scope
from memory.redaction import redact_credentials, text_hash
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.assistant_helpers import finish, next_turn
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_knowledge import seed as knowledge_seed
from tests.unit.assistant_helpers import checkpoint, events, projected_request
from tests.unit.test_assistant_reads import call_tool
from tool.tool import ToolContext


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)


@pytest.fixture
async def external_io(monkeypatch):
    """Real embedding/Qdrant adapters, only HTTP responses are substituted."""
    state = SimpleNamespace(hits=[], calls=[], before=None, fail=False)
    monkeypatch.setenv("MEMORY_BAILIAN_API_KEY", "isolated-test-key")

    async def respond(request):
        body = json.loads(request.content)
        kind = "qdrant" if request.url.path.endswith("/points/query") else "embedding"
        state.calls.append((kind, body))
        if state.before:
            await state.before(kind)
        if state.fail:
            return httpx.Response(503, json={"error": "isolated unavailable"})
        if kind == "embedding":
            assert set(body) == {"model", "input", "dimensions", "encoding_format"}
            return httpx.Response(200, json={"data": [
                {"index": i, "embedding": [0.1] * body["dimensions"]} for i in range(len(body["input"]))],
                "usage": {"total_tokens": 3}})
        requested = next(item["match"]["value"] for item in body["filter"]["must"] if item["key"] == "kind")
        return httpx.Response(200, json={"result": {"points": [
            {"payload": {"kind": hit.get("kind", "memory"), "object_id": hit["id"],
                         "revision": hit["revision"], "chunk_id": "0"}, "score": .9}
            for hit in state.hits if hit.get("kind", "memory") == requested]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(retrieval, "BailianEmbedding", lambda config: BailianEmbedding(config, client=client))
        monkeypatch.setattr(retrieval, "QdrantMemoryIndex", lambda config: QdrantMemoryIndex(config, client=client))
        yield state


def configure(monkeypatch, config):
    config.memory.retrieval_v2 = config.memory.v2_write = True
    config.memory.embedding_dimensions = 64
    config.memory.automatic_knowledge = False
    config.jwt_secret = "isolated-assistant-memory-pages"
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(memory, "get_config", lambda: config)
    monkeypatch.setattr("assistant.history.get_config", lambda: config)


async def seed(monkeypatch):
    identity, other, projects, config = await knowledge_seed(monkeypatch)
    configure(monkeypatch, config)
    return identity, other, projects, config


async def note(identity, summary="MEMORY_CANARY confirmed background", project_id=None):
    return await service.create_note(user_id=identity["user_id"], workspace_id=identity["workspace_id"],
                                     project_id=project_id, summary=summary)


async def sources(note_id):
    async with get_db_session() as db:
        row = await db.get(UserMemory, note_id)
        return list((await db.scalars(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).where(MemorySourceLink.memory_id == note_id,
            MemorySourceLink.revision == row.revision, MemorySourceLink.relation == "SUPPORTS"))).all())


async def reference(identity, value, **scope):
    result = await memory.search(**identity, query="MEMORY_CANARY", **scope)
    return next(item["source_ref"] for item in result["items"] if item["id"] == value["id"])


async def revoke(source_id):
    async with get_db_session() as db:
        (await db.get(MemorySource, source_id)).status = "REVOKED"


async def start(identity):
    async with get_db_session() as db:
        main = await db.get(Session, identity["main_id"])
    ctx = ToolContext(user_id=main.user_id, workspace_id=main.workspace_id, session_id=main.id,
                      project_id=main.project_id, agent_id="assistant")
    return await next_turn(ctx, "Consult my confirmed memories and original evidence. Only answer with text.")


async def test_hybrid_semantic_recall_explicit_owned_scope_and_ordinary_default_remain(monkeypatch, external_io):
    identity, other, projects, config = await seed(monkeypatch)
    personal = await note(identity, "Personal Sunday ceramics")
    a = await note(identity, "Project amber schedule", projects[0])
    b = await note(identity, "Project blue schedule", projects[1])
    foreign = await note({**identity, "user_id": other}, "FOREIGN_OWNER_CANARY", projects[2])
    external_io.hits = [personal, a, b, foreign]
    async with get_db_session() as db:
        (await db.get(Session, identity["main_id"])).project_id = projects[0]
    # No lexical overlap: the existing dense adapter supplies every candidate.
    plain = await memory.search(**identity, query="unmatched synonym")
    assert [item["id"] for item in plain["items"]] == [personal["id"]]
    selected = await memory.search(**identity, query="unmatched synonym", project_id=projects[0])
    assert {item["id"] for item in selected["items"]} == {personal["id"], a["id"]}
    every = await memory.search(**identity, query="unmatched synonym", include_all_projects=True)
    assert {item["id"] for item in every["items"]} == {personal["id"], a["id"], b["id"]}
    assert foreign["id"] not in json.dumps(every) and "FOREIGN_OWNER_CANARY" not in json.dumps(every)
    assert every["search"]["bounded"] and not every["search"]["exhaustive"]
    queries = [body for kind, body in external_io.calls if kind == "qdrant"]
    assert len(queries) == 6
    filters = [{clause["key"]: clause["match"] for clause in query["filter"]["must"]} for query in queries]
    assert filters[0]["project_id"] == {"any": [""]}
    assert filters[2]["project_id"] == {"any": ["", projects[0]]}
    assert projects[2] not in filters[4]["project_id"]["any"]
    assert [value["kind"]["value"] for value in filters] == ["memory", "source"] * 3
    assert all(value["user_id"] == {"value": identity["user_id"]}
               and value["workspace_id"] == {"value": identity["workspace_id"]} for value in filters)
    with pytest.raises(AssistantError):
        await memory.search(**identity, query="schedule", project_id=projects[2])
    with pytest.raises(ValueError):
        await memory.search(**identity, query="schedule", project_id=projects[0], include_all_projects=True)
    # The new strict loader is optional; old services keep their three kinds.
    external_io.calls.clear()
    ordinary = await retrieval.search_memory(user_id=identity["user_id"], workspace_id=identity["workspace_id"],
        query="ceramics", config=config.memory)
    assert personal["id"] in {item["id"] for item in ordinary["items"]}
    assert {next(clause["match"]["value"] for clause in body["filter"]["must"] if clause["key"] == "kind")
            for kind, body in external_io.calls if kind == "qdrant"} == {"memory", "source", "wiki"}
    assert config.memory.retrieval_v2 and not config.memory.automatic_knowledge


async def test_keyword_fallback_empty_observation_and_later_requests_never_retrieve_again(monkeypatch, external_io):
    identity, _, _, _ = await seed(monkeypatch)
    external_io.fail = True
    current = await note(identity)
    found = await memory.search(**identity, query="MEMORY_CANARY")
    assert found["items"][0]["id"] == current["id"] and found["search"]["degraded_reasons"]
    ctx, lease, answer = await start(identity)
    try:
        result, _, _ = await call_tool(ctx, "memory.search", {"query": "unmatched"})
        value = json.loads(result.output)
        assert value["status"] == "no_available_evidence" and value["items"] == []
        count = len(external_io.calls)
        later = await note(identity, "unmatched is a newly created fact")
        # The empty observation is used as read: building requests never searches again.
        assert "no_available_evidence" in json.dumps(await consume_context(ctx))
        await finish(ctx, lease, answer, "Nothing matched.")
        ctx, lease, _ = await next_turn(ctx)
        await consume_context(ctx)
        assert len(external_io.calls) == count
        assert (await memory.search(**identity, query="unmatched"))["items"][0]["id"] == later["id"]
    finally:
        await lease.release(session_status="idle")


async def test_assistant_tools_enforce_the_readers_current_owned_scope(monkeypatch, external_io):
    identity, other, projects, _ = await seed(monkeypatch)
    personal = await note(identity, "MEMORY_CANARY personal")
    project = await note(identity, "MEMORY_CANARY project", projects[0])
    foreign = await note({**identity, "user_id": other}, "MEMORY_CANARY FOREIGN_OWNER", projects[2])
    external_io.hits = [personal, project, foreign]
    ctx, lease, _ = await start(identity)
    try:
        default, _, _ = await call_tool(ctx, "memory.search", {"query": "MEMORY_CANARY"})
        assert [item["id"] for item in json.loads(default.output)["items"]] == [personal["id"]]
        every, _, _ = await call_tool(ctx, "memory.search", {"query": "MEMORY_CANARY", "include_all_projects": True})
        assert {item["id"] for item in json.loads(every.output)["items"]} == {personal["id"], project["id"]}
        assert "FOREIGN_OWNER" not in every.output
        denied, _, _ = await call_tool(ctx, "memory.search", {"query": "MEMORY_CANARY", "project_id": projects[2]})
        assert denied.metadata.get("error") and "FOREIGN_OWNER" not in denied.output
        ref = next(item["source_ref"] for item in json.loads(every.output)["items"] if item["id"] == project["id"])
        # A project reference needs that project (or all owned projects) selected again at read time.
        unscoped, _, _ = await call_tool(ctx, "memory.read", {"source_ref": ref})
        assert unscoped.metadata.get("error") and "MEMORY_CANARY project" not in unscoped.output
        scoped, _, _ = await call_tool(ctx, "memory.read", {"source_ref": ref, "project_id": projects[0]})
        assert not scoped.metadata.get("error") and json.loads(scoped.output)["text"] == "MEMORY_CANARY project"
        async with get_db_session() as db:
            (await db.get(Project, projects[0])).is_deleted = True
        gone, _, _ = await call_tool(ctx, "memory.read", {"source_ref": ref, "include_all_projects": True})
        assert gone.metadata.get("error") and "MEMORY_CANARY project" not in gone.output
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["source_revoked", "source_body", "source_metadata", "source_revision",
    "missing_support", "revision_hash", "memory_revision", "memory_summary", "cross_project", "expired", "candidate",
    "project_deleted", "actor_removed", "forget_memory", "forget_source"])
async def test_changed_or_forgotten_authority_hides_summary_and_blocks_original_ref(monkeypatch, external_io, change):
    identity, _, projects, _ = await seed(monkeypatch)
    value = await note(identity, project_id=projects[0])
    original = (await sources(value["id"]))[0]
    ref = await reference(identity, value, include_all_projects=True)
    first = await memory.read(**identity, source_ref=ref, include_all_projects=True, max_chars=4)
    if change.startswith("forget"):
        result = await service.forget_memory(user_id=identity["user_id"], workspace_id=identity["workspace_id"],
            memory_id=value["id"], expected_revision=value["revision"],
            mode="sources" if change == "forget_source" else "memory",
            source_ids=[original.id] if change == "forget_source" else None)
        assert result["ok"]
    else:
        async with get_db_session() as db:
            row, source = await db.get(UserMemory, value["id"]), await db.get(MemorySource, original.id)
            if change == "source_revoked": source.status = "REVOKED"
            elif change == "source_body": source.body = "changed without a hash"
            elif change == "source_metadata": source.source_metadata = {"different_admission": True}
            elif change == "source_revision": source.source_revision += 1
            elif change == "missing_support":
                await db.execute(delete(MemorySourceLink).where(MemorySourceLink.memory_id == row.id))
                revision = await db.scalar(select(MemoryRevision).where(MemoryRevision.memory_id == row.id))
                revision.source_set_hash = sha256(json.dumps([], sort_keys=True).encode()).hexdigest()
                scope = await resolve_access_scope(db, user_id=identity["user_id"],
                    workspace_id=identity["workspace_id"], project_id=projects[0])
                assert await service.memory_sources_available(db, scope, row)  # Legacy all([]) is insufficient.
            elif change == "revision_hash":
                revision = await db.scalar(select(MemoryRevision).where(MemoryRevision.memory_id == row.id))
                revision.source_set_hash = "0" * 64
            elif change == "memory_revision": row.revision += 1
            elif change == "memory_summary": row.value = {"summary": "forged unchanged version"}
            elif change == "cross_project": source.project_id = projects[1]
            elif change == "expired": row.ttl = datetime.now(timezone.utc) - timedelta(seconds=10)
            elif change == "candidate": row.confirmation_status = "UNCONFIRMED"
            elif change == "project_deleted": (await db.get(Project, projects[0])).is_deleted = True
            else: (await db.get(WorkspaceMember, (identity["workspace_id"], identity["user_id"]))).status = "removed"
    for cursor in (None, first["next_cursor"]):
        with pytest.raises(AssistantError):
            await memory.read(**identity, source_ref=ref, include_all_projects=True, max_chars=4, cursor=cursor)
    if change == "actor_removed":
        with pytest.raises(AssistantError): await memory.search(**identity, query="MEMORY_CANARY", include_all_projects=True)
    elif change != "source_metadata":
        found = await memory.search(**identity, query="MEMORY_CANARY", include_all_projects=True)
        assert value["id"] not in json.dumps(found) and "MEMORY_CANARY" not in json.dumps(found)
    # Metadata-only change remains readable under a *new* observation, never
    # authorizes the old exact reference by silently blessing new identity.


async def human_memory(monkeypatch, text="MEMORY_CANARY: I have ceramics every Sunday and prefer quiet studios."):
    from tests.unit.test_memory_pipeline import _finish_turn, _seed
    data = await _seed(monkeypatch)
    await _finish_turn(data, text=text)
    from core.config import get_config
    config = get_config()
    configure(monkeypatch, config)
    proposed = await service.write_memory(user_id=data[0], workspace_id=data[1], project_id=data[2],
        scope="LONG_TERM", type="PREFERENCE", value={"summary": "MEMORY_CANARY quiet Sunday ceramics"},
        owner="SYSTEM_INFERRED", evidence={"session_id": data[3]})
    confirmed = await service.confirm_note(user_id=data[0], workspace_id=data[1], proposal_id=proposed["id"],
                                          expected_revision=proposed["revision"])
    main = await ensure_main_session(user_id=data[0], workspace_id=data[1])
    identity = {"user_id": data[0], "workspace_id": data[1], "main_id": main.id}
    return identity, confirmed, data


@pytest.mark.parametrize("change", ["origin", "actor", "synthetic", "isolated", "deleted", "role", "span", "snapshot"])
async def test_confirmed_human_evidence_checks_real_original_admission(monkeypatch, external_io, change):
    identity, value, data = await human_memory(monkeypatch)
    ref = await reference(identity, value, include_all_projects=True)
    original = next(source for source in await sources(value["id"]) if source.source_kind == "user_statement")
    body = await memory.read(**identity, include_all_projects=True, source_ref=ref, source_id=original.id)
    assert "prefer quiet studios" in body["text"] and body["selected_source"]["session_id"] == data[3]
    async with get_db_session() as db:
        part = await db.get(Part, original.part_id)
        if change == "origin": part.data = {**part.data, "origin": "tool"}
        elif change == "actor": part.data = {**part.data, "origin_ref": {"actor_user_id": "not-the-actor"}}
        elif change == "synthetic": part.data = {**part.data, "synthetic": True}
        elif change == "isolated": (await db.get(Session, data[3])).memory_policy = "assistant_isolated"
        elif change == "deleted": (await db.get(Session, data[3])).is_deleted = True
        elif change == "role": (await db.get(Message, original.message_id)).role = "assistant"
        else:
            source = await db.get(MemorySource, original.id)
            if change == "span": source.source_metadata = {**source.source_metadata, "span_start": 1}
            else:
                source.body = "different stored text with unchanged original Part"
                source.content_hash = text_hash(source.body)
    if change in {"span", "snapshot"}:
        async with get_db_session() as db:
            scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
            assert await service.source_is_available(db, scope, await db.get(MemorySource, original.id))
    with pytest.raises(AssistantError):
        await memory.read(**identity, include_all_projects=True, source_ref=ref)
    assert not (await memory.search(**identity, include_all_projects=True, query="MEMORY_CANARY"))["items"]


async def test_original_message_excerpt_keeps_exact_span_without_claiming_unread_suffix(monkeypatch, external_io):
    original = "MEMORY_CANARY " + "x" * 32000 + " UNREAD_ORIGINAL_SUFFIX"
    identity, value, _ = await human_memory(monkeypatch, text=original)
    ref = await reference(identity, value, include_all_projects=True)
    source = next(source for source in await sources(value["id"]) if source.source_kind == "user_statement")
    first = await memory.read(**identity, include_all_projects=True, source_ref=ref, source_id=source.id, max_chars=16000)
    second = await memory.read(**identity, include_all_projects=True, source_ref=ref, source_id=source.id,
                               max_chars=16000, cursor=first["next_cursor"])
    assert first["text"] + second["text"] == original[:32000]
    assert first["selected_source"]["source_span"] == {
        "start": 0, "end": 32000, "total_chars": len(original), "complete": False}
    assert second["total_chars"] == 32000 and second["next_cursor"] is None and not second["truncated"]
    assert "UNREAD_ORIGINAL_SUFFIX" not in json.dumps([first, second])


async def test_forgetting_one_fact_preserves_other_summary_but_blocks_shared_raw_body(monkeypatch, external_io):
    identity, first, data = await human_memory(monkeypatch)
    proposed = await service.write_memory(user_id=data[0], workspace_id=data[1], project_id=data[2],
        scope="LONG_TERM", type="USER_PROFILE", value={"summary": "MEMORY_CANARY Sunday ceramics"},
        owner="SYSTEM_INFERRED", evidence={"session_id": data[3]})
    other = await service.confirm_note(user_id=data[0], workspace_id=data[1], proposal_id=proposed["id"],
                                       expected_revision=proposed["revision"])
    shared = next(source for source in await sources(other["id"]) if source.source_kind == "user_statement")
    prior = await reference(identity, other, include_all_projects=True)
    assert (await service.forget_memory(user_id=data[0], workspace_id=data[1], memory_id=first["id"],
        expected_revision=first["revision"], mode="memory"))["ok"]
    with pytest.raises(AssistantError): await memory.read(**identity, source_ref=prior, include_all_projects=True)
    current = await memory.search(**identity, query="MEMORY_CANARY", include_all_projects=True)
    assert {item["id"] for item in current["items"]} == {other["id"]}
    item = current["items"][0]
    assert next(source for source in item["sources"] if source["id"] == shared.id)["body_available"] is False
    assert (await memory.read(**identity, include_all_projects=True, source_ref=item["source_ref"]))["text"] == "MEMORY_CANARY Sunday ceramics"
    with pytest.raises(AssistantError):
        await memory.read(**identity, include_all_projects=True, source_ref=item["source_ref"], source_id=shared.id)


async def test_verified_revision_keeps_leaf_identity_and_original_correction(monkeypatch, external_io):
    from memory.extraction import MemoryExtractionWorker
    from tests.unit.test_automatic_knowledge import Verifier
    from tests.unit.test_memory_reconciliation import CHANGE, Planner, proposal, seed_correction
    data, value, _ = await seed_correction(monkeypatch)
    assert await MemoryExtractionWorker(extractor=proposal, verifier=Verifier(), reconciler=Planner(value["id"])).run_once() == "SUCCEEDED"
    from core.config import get_config
    configure(monkeypatch, get_config())
    main = await ensure_main_session(user_id=data[0], workspace_id=data[1])
    identity = {"user_id": data[0], "workspace_id": data[1], "main_id": main.id}
    current = await memory.search(**identity, query="周五", include_all_projects=True)
    item = next(item for item in current["items"] if item["id"] == value["id"])
    leaf = next(source for source in item["sources"] if source["kind"] == "user_statement")
    body = await memory.read(**identity, include_all_projects=True, source_ref=item["source_ref"], source_id=leaf["id"])
    assert body["text"] == CHANGE and leaf["relation"] == "dependency"
    async with get_db_session() as db:
        source = await db.get(MemorySource, leaf["id"])
        source.source_metadata = {**source.source_metadata, "rebound_identity": True}
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        derived = next(source for source in await sources(value["id"]) if source.source_kind == "verified_memory_revision")
        assert await service.source_is_available(db, scope, derived)
    with pytest.raises(AssistantError):
        await memory.read(**identity, include_all_projects=True, source_ref=item["source_ref"])


@pytest.mark.parametrize("body", ["原始正文🙂" * 4000, "\x01" * 15000,
    "lead password=fixture-credential-across-page-boundary after it more text"],
    ids=["unicode", "escaped-byte-budget", "redact-before-slice"])
async def test_read_full_summary_and_source_pages_are_bounded_redacted_and_read_only(monkeypatch, external_io, body):
    identity, _, _, _ = await seed(monkeypatch)
    value = await note(identity)
    source = (await sources(value["id"]))[0]
    # Simulate a persisted pre-redaction note without invoking a writer that
    # rightly rejects credentials. All original row/revision hashes are valid.
    async with get_db_session() as db:
        row = await db.get(UserMemory, value["id"])
        revision = await db.scalar(select(MemoryRevision).where(MemoryRevision.memory_id == row.id))
        row.value = revision.value = {"summary": body}
        row.content_hash = revision.content_hash = service.content_hash(body)
        original = await db.get(MemorySource, source.id)
        original.body, original.content_hash = body, text_hash(body)
        scope = await memory._access(db, **identity)
        item, _ = await memory._current(db, scope, identity["main_id"], row, {})
        ref = item["source_ref"]
    assert ReadArgs.model_validate({"source_ref": ref}).source_ref.kind == "memory"
    maximum = 11 if body.startswith("lead") else 16000
    statements = []
    def record(_conn, _cursor, statement, *_args): statements.append(statement)
    event.listen(get_engine().sync_engine, "before_cursor_execute", record)
    try:
        for source_id in (None, source.id):
            pages, cursor = [], None
            while True:
                page = await memory.read(**identity, source_ref=ref, source_id=source_id, max_chars=maximum, cursor=cursor)
                pages.append(page)
                assert len(json.dumps(page, ensure_ascii=False).encode()) <= memory.MAX_RESPONSE_BYTES
                assert page["offset"] == sum(len(old["text"]) for old in pages[:-1])
                assert page["chunk_hash"] == text_hash(page["text"])
                assert page["projection_hash"] == text_hash(redact_credentials(body))
                assert page["item"]["source_ref"]["content_hash"] == text_hash(body)
                assert 1 <= len(page["text"]) <= maximum
                cursor = page["next_cursor"]
                assert page["truncated"] is (cursor is not None)
                if cursor is None: break
            assert "".join(page["text"] for page in pages) == redact_credentials(body)
            assert pages[-1]["end_offset"] == pages[-1]["total_chars"]
            assert "fixture-credential" not in json.dumps(pages)
    finally:
        event.remove(get_engine().sync_engine, "before_cursor_execute", record)
    assert statements and not any(re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|DROP)\b", sql, re.I) for sql in statements)


async def test_cursor_scope_source_budget_and_expiry_cannot_be_rebound(monkeypatch, external_io):
    identity, _, projects, _ = await seed(monkeypatch)
    value = await note(identity, project_id=projects[0])
    ref = await reference(identity, value, include_all_projects=True)
    first = await memory.read(**identity, source_ref=ref, project_id=projects[0], max_chars=4)
    base = dict(identity, source_ref=ref, project_id=projects[0], max_chars=4, cursor=first["next_cursor"])
    for changed in ({"max_chars": 5}, {"project_id": None, "include_all_projects": True},
        {"source_id": first["item"]["sources"][0]["id"]}, {"source_id": "outside-evidence"},
        {"source_ref": {**ref, "revision": True}}, {"source_ref": {**ref, "extra": "unsafe"}},
        {"cursor": first["next_cursor"] + "tamper"}):
        with pytest.raises(AssistantError): await memory.read(**{**base, **changed})
    assert (await memory.read(**base))["offset"] == 4
    future = memory.time.time() + memory.CURSOR_TTL + 10
    monkeypatch.setattr(memory.time, "time", lambda: future)
    with pytest.raises(AssistantError): await memory.read(**base)
    fresh = dict(identity, source_ref=ref, project_id=projects[0], max_chars=4)
    assert (await memory.read(**fresh))["text"] == first["text"]
    await revoke(first["item"]["sources"][0]["id"])
    with pytest.raises(AssistantError): await memory.read(**fresh)


async def test_network_wait_revocation_drops_dense_and_lexical_candidate(monkeypatch, external_io):
    identity, _, _, _ = await seed(monkeypatch)
    value = await note(identity)
    external_io.hits = [value]
    source = (await sources(value["id"]))[0]
    async def changed(kind):
        if kind == "embedding": await revoke(source.id)
    external_io.before = changed
    found = await memory.search(**identity, query="MEMORY_CANARY")
    assert found["items"] == [] and found["status"] == "no_available_evidence"
    assert "MEMORY_CANARY" not in json.dumps(found)


@pytest.mark.parametrize("read_body", [False, True])
async def test_revocation_after_a_read_is_not_retroactive_and_later_requests_do_not_replay_it(monkeypatch, external_io, read_body):
    identity, _, _, _ = await seed(monkeypatch)
    value = await note(identity)
    ctx, lease, answer = await start(identity)
    try:
        result, _, _ = await call_tool(ctx, "memory.search", {"query": "MEMORY_CANARY"})
        assert not result.metadata.get("error"), result.output
        if read_body:
            ref = json.loads(result.output)["items"][0]["source_ref"]
            result, _, _ = await call_tool(ctx, "memory.read", {"source_ref": ref, "max_chars": 5})
            assert not result.metadata.get("error"), result.output
        surface, wire = await projected_request(ctx)
        assert "MEMORY_CANARY confirmed background" in json.dumps(wire)
        await revoke((await sources(value["id"]))[0].id)
        # D1: the provider checkpoint does not re-validate this run's own observation.
        await checkpoint(ctx, surface)
        [requested] = await events(ctx, "model.requested")
        assert requested.payload["assistant_context"] == {"version": 2, "mode": "ordinary"}
        await finish(ctx, lease, answer, "MEMORY_DERIVED_REPLY")
        ctx, lease, _ = await next_turn(ctx)
        _, wire = await projected_request(ctx)
        assert "MEMORY_CANARY" not in json.dumps(wire) and "MEMORY_DERIVED_REPLY" in json.dumps(wire)
        assert not (await memory.search(**identity, query="MEMORY_CANARY"))["items"]
    finally:
        await lease.release(session_status="idle")


async def test_memory_derived_task_records_no_derivation_and_forgetting_is_not_retroactive(monkeypatch, external_io):
    from agent.driver import reserve_run
    from assistant.scheduling import require_runnable
    from db.models.assistant import AssistantCommand
    from session.session import create_session
    identity, _, _, _ = await seed(monkeypatch)
    value = await note(identity)
    ctx, lease, answer = await start(identity)
    try:
        result, _, _ = await call_tool(ctx, "memory.search", {"query": "MEMORY_CANARY"})
        assert not result.metadata.get("error"), result.output
        ref = json.loads(result.output)["items"][0]["source_ref"]
        result, _, _ = await call_tool(ctx, "memory.read", {"source_ref": ref})
        assert not result.metadata.get("error"), result.output
        await consume_context(ctx)
        result, _, _ = await call_tool(ctx, "tasks.submit", {"project_id": ctx.project_id,
            "title": "Memory-derived draft", "instructions": "Draft text from my verified memory. Do not publish.",
            "source_message_ids": [answer.parent_id]})
        assert not result.metadata.get("error"), result.output
        receipt = json.loads(result.output)
        async with get_db_session() as db:
            command = await db.get(AssistantCommand, receipt["command_id"])
            # V2 records the triggering human message, not a derivation proof.
            assert "derivation" not in command.source_ref
            assert "MEMORY_CANARY" not in json.dumps(command.source_ref)
            assert (await db.get(Session, receipt["execution_session_id"])).memory_policy == "assistant_isolated"
        child = await create_session(user_id=ctx.user_id, workspace_id=ctx.workspace_id, parent_id=receipt["execution_session_id"])
        await require_runnable(child.id, ctx.user_id)
        assert (await service.forget_memory(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            memory_id=value["id"], expected_revision=value["revision"], mode="memory"))["ok"]
        # D1: forgetting affects later reads, not accepted work.
        with pytest.raises(AssistantError): await memory.read(**identity, source_ref=ref)
        await require_runnable(child.id, ctx.user_id)
        execution = await reserve_run(receipt["execution_session_id"], ctx.user_id)
        await execution.release(session_status="idle")
        async with get_db_session() as db:
            assert (await db.get(AgentInboxItem, receipt["inbox_id"])).state == "accepted"
    finally:
        await lease.release(session_status="idle")


async def test_search_only_reads_sql_and_old_tools_remain_normal_session_only(monkeypatch, external_io):
    from memory.session_policy import require_context_memory
    from memory.policy import MemoryAccessDenied
    from session.session import create_session
    from tests.unit.test_assistant_reads import TOOLS
    from tool.memory_tools import MemorySearchArgs, execute_memory_search
    identity, _, _, _ = await seed(monkeypatch)
    value = await note(identity)
    statements = []
    def record(_conn, _cursor, statement, *_args): statements.append(statement)
    event.listen(get_engine().sync_engine, "before_cursor_execute", record)
    try:
        current = await memory.search(**identity, query="MEMORY_CANARY")
    finally:
        event.remove(get_engine().sync_engine, "before_cursor_execute", record)
    assert statements and not any(re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|DROP)\b", sql, re.I) for sql in statements)
    assert current["items"][0]["id"] == value["id"]
    normal = await create_session(user_id=identity["user_id"], workspace_id=identity["workspace_id"])
    ctx = ToolContext(user_id=normal.user_id, workspace_id=normal.workspace_id, session_id=normal.id,
                      project_id=normal.project_id, agent_id="build")
    assert (await require_context_memory(ctx)).id == normal.id
    legacy = await execute_memory_search(MemorySearchArgs(query="MEMORY_CANARY"), ctx)
    assert value["id"] in legacy.output and not legacy.metadata.get("error")
    denied = await TOOLS["memory.search"].execute({"query": "MEMORY_CANARY"}, ctx)
    assert denied.metadata.get("error")
    ctx.session_id, ctx.agent_id = identity["main_id"], "assistant"
    with pytest.raises(MemoryAccessDenied): await require_context_memory(ctx)


async def test_observation_is_used_as_read_and_derived_answers_survive_revocation(monkeypatch, external_io):
    from assistant.history import read_history
    from assistant.public_history import public_messages
    from session.session import get_messages
    identity, _, _, _ = await seed(monkeypatch)
    value = await note(identity)
    ctx, lease, answer = await start(identity)
    try:
        result, _, _ = await call_tool(ctx, "memory.search", {"query": "MEMORY_CANARY"})
        assert not result.metadata.get("error"), result.output
        queries = len(external_io.calls)
        assert "MEMORY_CANARY" in json.dumps(await consume_context(ctx))
        await finish(ctx, lease, answer, "MEMORY_DERIVED_REPLY")
        scope = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id, session_id=ctx.session_id)
        await read_history(**scope, message_ids=[answer.id])
        assert len(external_io.calls) == queries
        ctx, lease, derived = await next_turn(ctx)
        wire = json.dumps(await consume_context(ctx))
        assert "MEMORY_DERIVED_REPLY" in wire and "MEMORY_CANARY" not in wire
        await finish(ctx, lease, derived, "MEMORY_SECOND_GENERATION")
        await revoke((await sources(value["id"]))[0].id)
        # D1: answers written before the revocation stay as written.
        page = json.dumps(await read_history(**scope, message_ids=[answer.id, derived.id]))
        assert "MEMORY_DERIVED_REPLY" in page and "MEMORY_SECOND_GENERATION" in page
        async with get_db_session() as db: main = await db.get(Session, ctx.session_id)
        public = await public_messages(main, await get_messages(ctx.session_id, user_id=ctx.user_id), actor_user_id=ctx.user_id)
        assert "MEMORY_DERIVED_REPLY" in json.dumps(public) and "MEMORY_SECOND_GENERATION" in json.dumps(public)
        assert "source_status" not in json.dumps(public)
        assert len(external_io.calls) == queries
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("mode", ["report_only", "coordination"])
async def test_memory_capabilities_do_not_open_restricted_modes(monkeypatch, external_io, mode):
    if mode == "report_only":
        from tests.unit.assistant_helpers import prepare_report
        ctx, lease, *_ = await prepare_report()
    else:
        from tests.unit.assistant_helpers import coordinator, ready
        values, task, result_id = await ready(monkeypatch)
        ctx, lease, _ = await coordinator(values, task, result_id, read=False)
    ref = {"version": 1, "kind": "memory", "id": "unselected", "assistant_session_id": ctx.session_id,
        "user_id": ctx.user_id, "workspace_id": ctx.workspace_id, "project_id": None, "visibility": "PERSONAL",
        "revision": 1, "content_hash": "a" * 64, "metadata_hash": "b" * 64, "dependencies_hash": "c" * 64, "acl_epoch": 1}
    try:
        for operation, args in (("memory.search", {"query": "memory"}), ("memory.read", {"source_ref": ref})):
            result, _, _ = await call_tool(ctx, operation, args)
            assert result.metadata["failure_code"] == "ASSISTANT_TOOL_FORBIDDEN"
        assert not external_io.calls
    finally:
        await lease.release(session_status="idle")


async def test_actual_loop_searches_and_reads_original_pages_and_later_runs_do_not_replay_them(monkeypatch, external_io):
    from agent import processor
    from session.agent_event_log import verify_agent_event_parity
    from tests.unit.test_agent_loop_terminal_steps import _assert_balanced_steps
    from tests.unit.assistant_helpers import _accept, _events, _run, _runtime
    state = await _runtime(monkeypatch)
    configure(monkeypatch, state.config)
    state.config.memory.allowed_user_ids = [state.owner]
    identity = {"user_id": state.owner, "workspace_id": state.workspace, "main_id": state.main.id}
    body = "MEMORY_BODY_CANARY original evidence. Another sentence is untrusted reference data."
    value = await note(identity, body)
    external_io.hits = [value]
    async def released_database(_kind):
        assert get_engine().sync_engine.pool.checkedout() == 0
    external_io.before = released_database
    calls, args, chunks = [], {}, []
    def output(kwargs, call_id):
        return json.loads(next(message["content"] for message in kwargs["messages"]
            if message["role"] == "tool" and message["tool_call_id"] == call_id))
    async def provider(**kwargs):
        calls.append(kwargs["messages"])
        number = len(calls)
        if number == 1:
            operation, arguments, call_id = "memory.search", {"query": "semantic unmatched"}, "memory-search"
        elif number == 2:
            item = output(kwargs, "memory-search")["items"][0]
            assert item["id"] == value["id"] and item["sources"][0]["origin"]
            args.update(source_ref=item["source_ref"], source_id=item["sources"][0]["id"], max_chars=50)
            operation, arguments, call_id = "memory.read", args, "memory-first"
        elif number == 3:
            first = output(kwargs, "memory-first")
            chunks.append(first["text"])
            assert first["selected_source"]["kind"] == "manual" and first["untrusted_data"] is True
            operation, arguments, call_id = "memory.read", {**args, "cursor": first["next_cursor"]}, "memory-second"
        elif number == 4:
            second = output(kwargs, "memory-second")
            chunks.append(second["text"])
            assert second["next_cursor"] is None and "".join(chunks) == body
        else:
            # D1: the saved reply stays; the forgotten evidence is not replayed.
            assert "MEMORY_BODY_CANARY" not in json.dumps(kwargs["messages"])
            assert "MEMORY_DERIVED_REPLY" in json.dumps(kwargs["messages"])
        if number <= 3:
            wire = next(name for name, tool in kwargs["tools"].items() if tool.id == operation)
            yield {"type": "tool_call", "tool": wire, "args": deepcopy(arguments), "call_id": call_id, "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            yield {"type": "text_delta", "text": "MEMORY_DERIVED_REPLY" if number == 4 else "The original source is unavailable."}
            yield {"type": "finish", "reason": "stop", "usage": {}}
    monkeypatch.setattr(processor, "stream_llm", provider)
    accepted = await _accept(state, "Search my confirmed memory and read its original evidence completely.")
    await _run(state)
    assert len(calls) == 4
    assert [kind for kind, _ in external_io.calls] == ["embedding", "qdrant", "qdrant"]
    requested = await _events(state, "model.requested")
    # V2 checkpoints record the turn mode only; nothing is captured for re-validation.
    assert [entry.payload["assistant_context"] for entry in requested] == [{"version": 2, "mode": "ordinary"}] * 4
    assert not await _events(state, "assistant.context.consumed")
    async with get_db_session() as db:
        receipt = await db.get(AgentInboxItem, accepted["inbox_id"])
        assert receipt.state == "settled" and receipt.outcome == "succeeded"
        answer = await db.get(Message, receipt.result_message_id)
        assert answer.finish == "stop" and not answer.error
        parts = list((await db.scalars(select(Part).where(Part.session_id == state.main.id, Part.type == "tool"))).all())
        assert len(parts) == 3 and all("transient_assistant_refs" not in json.dumps(part.data) for part in parts)
        assert (await db.get(Session, state.main.id)).memory_policy == "assistant_isolated"
    await _assert_balanced_steps(state.main.id, state.owner, 4)
    assert (await verify_agent_event_parity(state.main.id, user_id=state.owner)).ok
    await revoke((await sources(value["id"]))[0].id)
    await _accept(state, "What was verified in the prior reply?")
    await _run(state)
    # V2 P3: every main turn also retrieves the user's profile and relevant
    # memories, so retrieval runs per turn besides the explicit memory.search.
    kinds = [kind for kind, _ in external_io.calls]
    assert len(calls) == 5 and {"embedding", "qdrant"} <= set(kinds) and len(kinds) > 3
