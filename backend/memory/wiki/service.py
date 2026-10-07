"""SQL authority for source-grounded Wiki derivatives and approval CAS.

Read paths never call models or repair indexes. Every read and publish checks
current source/memory admission, hashes and membership epoch, even if a worker
has not yet propagated an invalidation event.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import re

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from core.identifier import ascending
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemoryIndexState, MemoryOutbox, MemorySource, MemorySourceLink, MemoryTombstone
from db.models.memory_wiki import MemoryWikiCandidate, MemoryWikiDependency, MemoryWikiJob, MemoryWikiPage
from memory.index.base import DocumentSnapshot
from memory.policy import MemoryAccessScope, active_memory_predicates, resolve_access_scope
from memory.redaction import json_hash
from memory.service import content_hash, lock_memory_authority, source_body_is_available
from wiki_compiler import CompilePolicy, CompileRequest, SourceSnapshot, TargetSnapshot, candidate_hash
from wiki_compiler.hashing import text_hash
from wiki_compiler.contracts import CONTRACT_VERSION


class WikiStateError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


async def _occurred_at(db, scope, source):
    from memory.source_time import source_occurred_at
    value = await source_occurred_at(db, access=scope, source=source)
    if value is None:
        return None
    # SQLite returns naive values for timestamps stored in UTC.
    return (value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)).isoformat()


def domain_for(scope: MemoryAccessScope, project_id=None) -> str:
    return json_hash({"actor": scope.actor_user_id, "workspace": scope.workspace_id, "project": project_id})


def target_identity(scope, slug: str, project_id=None) -> str:
    return json_hash({"domain": domain_for(scope, project_id), "slug": slug})


def _source_ref(source, scope):
    return {"kind": "source", "id": source.id, "revision": source.source_revision,
            "content_hash": source.content_hash, "acl_epoch": source.acl_epoch,
            "scope_epoch": scope.acl_epoch}


def immutable_candidate_hash(draft, sources, memories, acl_epoch, target_revision, target_hash):
    return json_hash({"contract": "wiki-host-candidate-v1", "draft": draft, "source_manifest": sources,
                     "memory_manifest": memories, "acl_epoch": acl_epoch,
                     "expected_target_revision": target_revision, "expected_target_hash": target_hash})


def _candidate_intact(candidate):
    from memory.wiki.automatic import COMPLETE_TOPIC_POLICY
    return (candidate.draft.get("schema_version") == CONTRACT_VERSION
        and candidate.draft.get("policy_version") in {CompilePolicy(model=candidate.draft.get("model", "")).version, COMPLETE_TOPIC_POLICY}
        and candidate_hash(candidate.draft) == candidate.draft.get("candidate_hash")
        and immutable_candidate_hash(candidate.draft, candidate.source_manifest, candidate.memory_manifest,
            candidate.acl_epoch, candidate.expected_target_revision, candidate.expected_target_hash) == candidate.candidate_hash)


async def _target(db, scope, identity, *, lock=False):
    stmt = select(MemoryWikiPage).where(MemoryWikiPage.target_identity == identity,
                                      *scope.predicates(MemoryWikiPage))
    return await db.scalar(stmt.with_for_update() if lock else stmt)


async def target_is_deleted(db, page) -> bool:
    return bool(page and (page.deleted_at or await db.scalar(select(MemoryTombstone.id).where(
        MemoryTombstone.object_kind == "wiki", MemoryTombstone.object_id == page.id))))


async def collect_compile_sources(db, scope, memory_ids=None):
    if memory_ids is not None and (not memory_ids or len(memory_ids) > 12 or len(set(memory_ids)) != len(memory_ids)):
        raise WikiStateError("wiki_source_budget_exceeded")
    stmt = select(UserMemory).where(*scope.predicates(UserMemory), *active_memory_predicates())
    if memory_ids:
        stmt = stmt.where(UserMemory.id.in_(memory_ids))
    rows = list((await db.scalars(stmt.order_by(UserMemory.updated_at.desc(), UserMemory.id).limit(12))).all())
    if memory_ids and len(rows) != len(memory_ids):
        raise WikiStateError("wiki_source_unavailable")
    sources, memories = {}, []
    for row in rows:
        summary = (row.value or {}).get("summary", "")
        if not summary or row.content_hash != content_hash(summary):
            raise WikiStateError("wiki_source_hash_mismatch")
        links = (await db.execute(select(MemorySourceLink, MemorySource).join(MemorySource,
            MemorySource.id == MemorySourceLink.source_id).where(MemorySourceLink.memory_id == row.id,
            MemorySourceLink.revision == row.revision, MemorySourceLink.relation == "SUPPORTS"))).all()
        used = []
        for link, source in links:
            if (source.source_revision != link.source_revision or not source.body
                    or not await source_body_is_available(db, scope, source)
                    or source.content_hash != text_hash(source.body)
                    or source.source_kind in {"wiki", "memory", "assistant_inference"}):
                raise WikiStateError("wiki_source_unavailable")
            sources[source.id] = source
            used.append(source.id)
        if used:
            memories.append({"kind": "memory", "id": row.id, "revision": row.revision,
                             "content_hash": row.content_hash, "source_ids": sorted(used)})
    if not sources:
        raise WikiStateError("wiki_no_confirmed_sources")
    if len(sources) > 12 or sum(len(source.body) for source in sources.values()) > 16000:
        raise WikiStateError("wiki_source_budget_exceeded")
    return sources, memories


async def freeze_compile(db, scope, *, slug: str, title: str, model: str,
                         memory_ids: list[str] | None = None) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", slug) or not title.strip() or len(title) > 160:
        raise WikiStateError("invalid_wiki_target")
    sources, memories = await collect_compile_sources(db, scope, memory_ids)
    identity = target_identity(scope, slug, scope.project_id)
    page = await _target(db, scope, identity)
    if page and page.status == "REDIRECT":
        raise WikiStateError("wiki_target_merged")
    if await target_is_deleted(db, page):
        raise WikiStateError("wiki_target_deleted")
    if page and page.body is not None and text_hash(page.body) != page.content_hash:
        raise WikiStateError("wiki_target_hash_mismatch")
    from db.models.wiki_platform import WikiConcept
    from memory.wiki.automatic import COMPLETE_TOPIC_POLICY
    concept = await db.scalar(select(WikiConcept).where(*scope.predicates(WikiConcept),
        WikiConcept.page_id == (page.id if page else f"wiki_{identity[:40]}"), WikiConcept.status == "ACTIVE"))
    policy = CompilePolicy(model=model, version=COMPLETE_TOPIC_POLICY) if (
        concept and concept.extra_metadata.get("consolidations")) else CompilePolicy(model=model)
    request = CompileRequest(slug, title.strip(), domain_for(scope, scope.project_id),
        tuple(SourceSnapshot(source.id, source.source_revision, source.body, source.content_hash,
                             domain_for(scope, scope.project_id), scope.acl_epoch)
              for source in sorted(sources.values(), key=lambda source: source.id)),
        TargetSnapshot(page.id if page else f"wiki_{identity[:40]}", page.revision if page else 0,
                       page.content_hash if page else None), policy)
    from wiki_compiler.compiler import validate_request
    validate_request(request)
    manifest = [_source_ref(source, scope) for source in sorted(sources.values(), key=lambda source: source.id)]
    # Target CAS is separate from content identity: an unchanged published page
    # can be reused without rerunning the model after its initial approval.
    input_hash = json_hash({"domain": request.domain, "slug": slug, "title": title.strip(),
        "sources": manifest, "memories": sorted(memories, key=lambda item: item["id"]),
        "policy": asdict(request.policy)})
    return {"request": request, "sources": manifest, "memories": memories, "input_hash": input_hash,
            "target_identity": identity, "page": page}


class ReadAhead:
    """The rows read_sources checks for many pages, one statement per table.

    For read-only passes: each statement has the predicates of read_sources'
    own per-row reads, so every check sees the row it would have read itself.
    """

    def __init__(self):
        self.memories, self.links, self.sources, self.tombstoned = {}, {}, {}, set()

    @classmethod
    async def read(cls, db, scope, pages):
        ahead = cls()
        memory_ids = sorted({memory["id"] for page in pages for memory in page.memory_manifest or []
                             if memory.get("kind") not in ("document", "exchange")})
        source_ids = sorted({reference["id"] for page in pages for reference in page.source_manifest or []})
        for batch in _batches(memory_ids):
            ahead.memories.update({row.id: row for row in (await db.scalars(select(UserMemory).where(
                UserMemory.id.in_(batch), *scope.predicates(UserMemory), *active_memory_predicates()))).all()})
        for batch in _batches(sorted(ahead.memories)):
            for memory_id, revision, source_id in (await db.execute(select(MemorySourceLink.memory_id,
                    MemorySourceLink.revision, MemorySourceLink.source_id).where(
                    MemorySourceLink.memory_id.in_(batch), MemorySourceLink.relation == "SUPPORTS"))).all():
                ahead.links.setdefault((memory_id, revision), set()).add(source_id)
        for batch in _batches(source_ids):
            ahead.sources.update({row.id: row for row in (await db.scalars(select(MemorySource).where(
                MemorySource.id.in_(batch), *scope.predicates(MemorySource)))).all()})
        for batch in _batches(sorted({page.id for page in pages})):
            ahead.tombstoned.update((await db.scalars(select(MemoryTombstone.object_id).where(
                MemoryTombstone.object_kind == "wiki", MemoryTombstone.object_id.in_(batch)))).all())
        documents = {memory["id"] for page in pages for memory in page.memory_manifest or []
                     if memory.get("kind") == "document"}
        if documents:
            from memory.documents.authority import prefetch_revisions
            await prefetch_revisions(db, scope, documents)
        return ahead


def _batches(ids, size=500):
    return [ids[start:start + size] for start in range(0, len(ids), size)]


async def read_sources(db, scope, sources, memories, *, acl_epoch: int, lock=False, ahead: ReadAhead | None = None):
    if ahead is not None and lock:
        raise ValueError("rows read ahead cannot stand in for locked reads")
    if scope.acl_epoch != acl_epoch:
        raise WikiStateError("wiki_acl_changed")
    by_id = {}
    for memory in memories:
        if memory.get("kind") == "document":
            from memory.documents.authority import read_dependency
            await read_dependency(db, scope, memory, lock=lock)
            continue
        if memory.get("kind") == "exchange":
            from memory.wiki.exchange import read_import_dependency
            await read_import_dependency(db, scope, memory, lock=lock)
            continue
        if ahead is not None:
            row = ahead.memories.get(memory["id"])
        else:
            stmt = select(UserMemory).where(UserMemory.id == memory["id"], *scope.predicates(UserMemory),
                                           *active_memory_predicates())
            row = await db.scalar(stmt.with_for_update() if lock else stmt)
        if row is None or row.revision != memory["revision"] or row.content_hash != memory["content_hash"]:
            raise WikiStateError("wiki_source_changed")
        if content_hash((row.value or {}).get("summary", "")) != row.content_hash:
            raise WikiStateError("wiki_source_hash_mismatch")
        if ahead is not None:
            ids = ahead.links.get((row.id, row.revision), set())
        else:
            ids = set((await db.scalars(select(MemorySourceLink.source_id).where(
                MemorySourceLink.memory_id == row.id, MemorySourceLink.revision == row.revision,
                MemorySourceLink.relation == "SUPPORTS"))).all())
        if not set(memory["source_ids"]).issubset(ids):
            raise WikiStateError("wiki_source_changed")
    admitted = {source_id for memory in memories for source_id in memory["source_ids"]}
    for reference in sources:
        if reference["id"] not in admitted:
            raise WikiStateError("wiki_source_unavailable")
        if ahead is not None:
            source = ahead.sources.get(reference["id"])
        else:
            stmt = select(MemorySource).where(MemorySource.id == reference["id"], *scope.predicates(MemorySource))
            source = await db.scalar(stmt.with_for_update() if lock else stmt)
        if (source is None or source.source_revision != reference["revision"]
                or source.content_hash != reference["content_hash"] or source.acl_epoch != reference["acl_epoch"]
                or not source.body or text_hash(source.body) != reference["content_hash"]
                or not await source_body_is_available(db, scope, source)):
            raise WikiStateError("wiki_source_changed")
        by_id[source.id] = source
    if not by_id or not memories:
        raise WikiStateError("wiki_source_unavailable")
    return by_id


async def dependencies_current(db, scope, row, *, lock=False) -> bool:
    try:
        await read_sources(db, scope, row.source_manifest, row.memory_manifest, acl_epoch=row.acl_epoch, lock=lock)
        return True
    except WikiStateError:
        return False


async def enqueue_page_outbox(db, page, config, operation="UPSERT"):
    generations = list((await db.scalars(select(MemoryIndexState.index_generation).where(
        MemoryIndexState.object_kind == "wiki", MemoryIndexState.object_id == page.id))).all())
    if config.index_generation not in generations:
        generations.append(config.index_generation)
    instant = now()
    for generation in generations:
        event_id = f"wiki:{page.id}:{page.revision}:{operation}:{generation}"
        if not await db.scalar(select(MemoryOutbox.id).where(MemoryOutbox.event_id == event_id)):
            db.add(MemoryOutbox(id=ascending("memory_outbox"), event_id=event_id,
                user_id=page.user_id, workspace_id=page.workspace_id, project_id=page.project_id,
                object_kind="wiki", object_id=page.id, revision=page.revision, operation=operation,
                index_generation=generation, priority=100 if operation != "UPSERT" else 0,
                payload={}, status="PENDING", attempts=0, lease_generation=0,
                available_at=instant, created_at=instant, updated_at=instant))
        state = await db.scalar(select(MemoryIndexState).where(MemoryIndexState.object_kind == "wiki",
            MemoryIndexState.object_id == page.id, MemoryIndexState.index_generation == generation).with_for_update())
        if state is None:
            state = MemoryIndexState(id=ascending("memory_index"), object_kind="wiki", object_id=page.id,
                index_generation=generation, user_id=page.user_id, workspace_id=page.workspace_id,
                project_id=page.project_id, desired_revision=page.revision, indexed_revision=0, chunk_ids=[],
                status="PENDING", updated_at=instant)
            db.add(state)
        state.desired_revision = page.revision
        state.status = "DELETE_PENDING" if operation != "UPSERT" else "PENDING"
        state.updated_at = instant


async def invalidate_memory_dependencies(db, *, memory_ids=(), source_ids=(), reason="source_changed"):
    """Called inside the source mutation transaction, before committing it."""
    from memory.wiki.organization import invalidate_concepts
    await invalidate_concepts(db, memory_ids=memory_ids, source_ids=source_ids)
    clauses = []
    if memory_ids:
        clauses.append((MemoryWikiDependency.object_kind == "memory") & MemoryWikiDependency.object_id.in_(memory_ids))
    if source_ids:
        clauses.append((MemoryWikiDependency.object_kind == "source") & MemoryWikiDependency.object_id.in_(source_ids))
    if not clauses:
        return
    dependency_ids = select(MemoryWikiDependency.page_id).where(or_(*clauses))
    pages = list((await db.scalars(select(MemoryWikiPage).where(MemoryWikiPage.id.in_(dependency_ids),
        MemoryWikiPage.status == "PUBLISHED").with_for_update())).all())
    from core.config import get_config
    config = get_config().memory
    for page in pages:
        page.status, page.invalidation_reason, page.updated_at = "STALE", reason, now()
        # Derived prose can contain the forgotten value; discard it immediately.
        page.body, page.paragraphs = None, []
        await enqueue_page_outbox(db, page, config, "DELETE")
    candidates = list((await db.scalars(select(MemoryWikiCandidate).where(MemoryWikiCandidate.status == "PENDING"))).all())
    for candidate in candidates:
        if ({item["id"] for item in candidate.memory_manifest} & set(memory_ids)
                or {item["id"] for item in candidate.source_manifest} & set(source_ids)):
            candidate.status, candidate.reason_code = "STALE", reason
    await db.flush()


async def authorized_wiki_documents(db, scope, config, *, only=None) -> list[DocumentSnapshot]:
    if not config.enabled("wiki", scope.actor_user_id):
        return []
    stmt = select(MemoryWikiPage).where(*scope.predicates(MemoryWikiPage), MemoryWikiPage.status == "PUBLISHED",
                                      MemoryWikiPage.deleted_at.is_(None))
    if only is not None:
        stmt = stmt.where(MemoryWikiPage.id.in_([object_id for kind, object_id in only if kind == "wiki"]))
    pages = list((await db.scalars(stmt.order_by(MemoryWikiPage.updated_at.desc()).limit(100))).all())
    ahead = await ReadAhead.read(db, scope, pages)
    if ahead.sources:
        from memory.service import prefetch_source_facts
        await prefetch_source_facts(db, scope, list(ahead.sources.values()))
    result = []
    for page in pages:
        if not page.body or page.content_hash != text_hash(page.body):
            continue
        try:
            sources = await read_sources(db, scope, page.source_manifest, page.memory_manifest,
                                         acl_epoch=page.acl_epoch, ahead=ahead)
        except WikiStateError:
            continue
        if page.id in ahead.tombstoned:
            continue
        # Enrich legacy manifests at read time from the same authorized SQL
        # snapshots, without changing the immutable approval envelope.
        source_refs = tuple([{**reference, "occurred_at": await _occurred_at(db, scope, sources[reference["id"]]),
            **({"document_id": sources[reference["id"]].source_metadata["document_id"], "origin_kind": "document_chunk"}
                if sources[reference["id"]].source_kind == "document_chunk" else {})}
                             for reference in page.source_manifest])
        result.append(DocumentSnapshot("wiki", page.id, page.revision, page.body, page.user_id, page.workspace_id,
            page.project_id, scope.acl_epoch, page.content_hash, source_refs,
            confirmation_status="UPLOADED_DOCUMENT" if any(ref.get("document_id") for ref in source_refs) else "CONFIRMED"))
    return result


def _job_view(row):
    return {"id": row.id, "status": row.status.lower(), "candidate_id": row.candidate_id,
            "attempts": row.attempts, "lease_generation": row.lease_generation,
            "reason_code": row.last_error, "usage": row.usage, "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(), "project_id": row.project_id}


async def schedule_compile(*, user_id, workspace_id, project_id, slug, title, memory_ids=None,
                           request_id=None, config=None, organization_id=None, maintenance_id=None,
                           expected_page_revision=None):
    from core.config import get_config
    full_config = get_config()
    config = config or full_config.memory
    if not config.enabled("wiki", user_id):
        raise WikiStateError("wiki_disabled")
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        if organization_id:
            from db.models.wiki_platform import WikiOrganizationRun
            organization = await db.scalar(select(WikiOrganizationRun).where(
                WikiOrganizationRun.id == organization_id, *scope.predicates(WikiOrganizationRun),
                WikiOrganizationRun.project_id == project_id))
            if organization is None or organization.status not in {"PENDING", "RUNNING", "RETRY"}:
                raise WikiStateError("wiki_organization_unavailable")
        if request_id:
            previous = await db.scalar(select(MemoryWikiJob).where(MemoryWikiJob.user_id == user_id,
                MemoryWikiJob.workspace_id == scope.workspace_id, MemoryWikiJob.request_id == request_id))
            if previous:
                # An idempotency key cannot replace the frozen target/scope.
                if (previous.project_id != project_id or previous.spec.get("slug") != slug or previous.spec.get("title") != title.strip()
                        or memory_ids and sorted(memory_ids) != sorted(item["id"] for item in previous.spec["memories"])):
                    raise WikiStateError("wiki_request_conflict")
                return _job_view(previous)
        frozen = await freeze_compile(db, scope, slug=slug, title=title, memory_ids=memory_ids,
                                      model=config.extract_model or full_config.model)
        page = frozen["page"]
        if expected_page_revision is not None and (page.revision if page else 0) != expected_page_revision:
            raise WikiStateError("wiki_target_changed")
        if page and page.input_hash == frozen["input_hash"] and page.status == "PUBLISHED" and page.body and page.content_hash == text_hash(page.body) and await dependencies_current(db, scope, page):
            return {"status": "unchanged", "page_id": page.id, "revision": page.revision, "model_called": False}
        pending = await db.scalar(select(MemoryWikiJob).where(MemoryWikiJob.target_identity == frozen["target_identity"],
            MemoryWikiJob.input_hash == frozen["input_hash"], MemoryWikiJob.status.in_(["PENDING", "RUNNING", "RETRY", "COMPLETED"])).order_by(MemoryWikiJob.created_at.desc()))
        request = frozen["request"]
        if pending and pending.spec.get("target") == asdict(request.target):
            candidate = await db.get(MemoryWikiCandidate, pending.candidate_id) if pending.status == "COMPLETED" else None
            if pending.status != "COMPLETED" or (candidate and candidate.status == "PENDING"
                    and _candidate_intact(candidate) and await dependencies_current(db, scope, candidate)):
                return _job_view(pending)
        instant = now()
        spec = {"schema_version": "wiki-job-v1", "slug": slug, "title": title.strip(), "domain": request.domain,
                "target": asdict(request.target), "policy": asdict(request.policy), "sources": frozen["sources"],
                "memories": frozen["memories"], "acl_epoch": scope.acl_epoch}
        if organization_id:
            spec["organization_id"] = organization_id
        elif maintenance_id:
            spec["maintenance_id"] = maintenance_id
        job = MemoryWikiJob(id=ascending("wiki_job"), user_id=user_id, workspace_id=scope.workspace_id,
            project_id=project_id, target_identity=frozen["target_identity"], input_hash=frozen["input_hash"],
            request_id=request_id, spec=spec, status="PENDING", attempts=0, lease_generation=0,
            available_at=instant, created_at=instant, updated_at=instant)
        db.add(job)
        await db.flush()
        return _job_view(job)


async def approve_candidate(*, user_id, workspace_id, candidate_id, candidate_revision, approved_hash,
                            expected_target_revision, expected_target_hash, request_id=None, config=None):
    from core.config import get_config
    config = config or get_config().memory
    if not config.enabled("wiki", user_id):
        raise WikiStateError("wiki_disabled")
    async with get_db_session() as db:
        return await publish_candidate_in_session(db, user_id=user_id, workspace_id=workspace_id,
            candidate_id=candidate_id, candidate_revision=candidate_revision, approved_hash=approved_hash,
            expected_target_revision=expected_target_revision, expected_target_hash=expected_target_hash,
            request_id=request_id, config=config)


async def publish_candidate_in_session(db, *, user_id, workspace_id, candidate_id, candidate_revision,
        approved_hash, expected_target_revision, expected_target_hash, request_id=None, config, automatic=False):
    """One atomic source/target CAS for both user edits and verified automation."""
    await lock_memory_authority(db, user_id=user_id)
    scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
    candidate = await db.scalar(select(MemoryWikiCandidate).where(MemoryWikiCandidate.id == candidate_id,
        *scope.predicates(MemoryWikiCandidate)).with_for_update())
    if candidate is None:
        return None
    project_scope = await resolve_access_scope(db, user_id=user_id, workspace_id=scope.workspace_id,
                                              project_id=candidate.project_id)
    if candidate.revision != candidate_revision or candidate.candidate_hash != approved_hash or not _candidate_intact(candidate):
        raise WikiStateError("wiki_candidate_changed")
    if candidate.expected_target_revision != expected_target_revision or candidate.expected_target_hash != expected_target_hash:
        raise WikiStateError("wiki_target_changed")
    if candidate.status == "APPROVED" and request_id and candidate.request_id == request_id:
        page = await _target(db, project_scope, candidate.target_identity)
        return await _page_view(db, project_scope, page) if page else None
    if candidate.status != "PENDING":
        raise WikiStateError("wiki_candidate_unavailable")
    await read_sources(db, project_scope, candidate.source_manifest, candidate.memory_manifest,
                       acl_epoch=candidate.acl_epoch, lock=True)
    page = await _target(db, project_scope, candidate.target_identity, lock=True)
    if ((page is None) != (expected_target_revision == 0)
            or page and (await target_is_deleted(db, page) or page.revision != expected_target_revision or page.content_hash != expected_target_hash
                         or page.body is not None and text_hash(page.body) != page.content_hash)):
        raise WikiStateError("wiki_target_changed")
    draft = candidate.draft
    instant = now()
    if page is None:
        page = MemoryWikiPage(id=candidate.target_page_id, target_identity=candidate.target_identity,
            user_id=user_id, workspace_id=scope.workspace_id, project_id=candidate.project_id,
            slug=draft["slug"], title=draft["title"], revision=1, content_hash=text_hash(draft["body"]),
            status="PUBLISHED", acl_epoch=project_scope.acl_epoch, policy_version=draft["policy_version"],
            model=draft["model"], input_hash=candidate.input_hash, candidate_id=candidate.id,
            created_at=instant, updated_at=instant)
        db.add(page)
    else:
        # The row lock and mandatory revision/hash prevent approval from
        # replacing a page created or changed after this candidate froze.
        page.revision += 1
    page.body, page.paragraphs = draft["body"], draft["paragraphs"]
    page.title, page.content_hash, page.status = draft["title"], text_hash(draft["body"]), "PUBLISHED"
    page.source_manifest, page.memory_manifest = candidate.source_manifest, candidate.memory_manifest
    page.acl_epoch, page.policy_version, page.model = project_scope.acl_epoch, draft["policy_version"], draft["model"]
    page.input_hash, page.candidate_id, page.updated_at = candidate.input_hash, candidate.id, instant
    page.invalidation_reason = None
    candidate.status, candidate.decided_at = "APPROVED", instant
    candidate.approved_by = None if automatic else user_id
    candidate.reason_code = "automatic_grounded" if automatic else "user_approved"
    candidate.request_id = request_id
    try:
        await db.flush()
    except IntegrityError as exc:
        raise WikiStateError("wiki_target_changed") from exc
    for dependency in candidate.source_manifest + candidate.memory_manifest:
        db.add(MemoryWikiDependency(id=ascending("wiki_dep"), page_id=page.id, page_revision=page.revision,
            object_kind=dependency["kind"], object_id=dependency["id"], object_revision=dependency["revision"],
            content_hash=dependency["content_hash"]))
    await enqueue_page_outbox(db, page, config)
    return await _page_view(db, project_scope, page)

async def _page_view(db, scope, page):
    valid = (page.status == "PUBLISHED" and not await target_is_deleted(db, page) and page.body
             and page.content_hash == text_hash(page.body) and await dependencies_current(db, scope, page))
    return {"id": page.id, "slug": page.slug, "title": page.title, "revision": page.revision,
            "content_hash": page.content_hash, "project_id": page.project_id,
            "status": page.status.lower() if valid or page.status != "PUBLISHED" else "stale",
            "body": page.body if valid else None, "body_available": bool(valid),
            "paragraphs": page.paragraphs if valid else [], "sources": page.source_manifest if valid else [],
            "memory_dependencies": page.memory_manifest if valid else [],
            "reason_code": page.invalidation_reason or (None if valid else "wiki_dependency_unavailable"),
            "updated_at": page.updated_at.isoformat(), "model": page.model, "candidate_id": page.candidate_id}


async def _candidate_view(db, scope, candidate):
    valid = await dependencies_current(db, scope, candidate) and _candidate_intact(candidate)
    draft = candidate.draft
    return {"id": candidate.id, "job_id": candidate.job_id, "revision": candidate.revision,
            "candidate_hash": candidate.candidate_hash, "project_id": candidate.project_id,
            "title": draft.get("title"), "slug": draft.get("slug"), "model": draft.get("model"),
            "body": draft.get("body") if valid else None, "body_available": valid,
            "paragraphs": draft.get("paragraphs", []) if valid else [],
            "sources": candidate.source_manifest if valid else [],
            "status": candidate.status.lower() if valid else "stale", "usage": candidate.usage,
            "expected_target_revision": candidate.expected_target_revision,
            "expected_target_hash": candidate.expected_target_hash,
            "reason_code": candidate.reason_code or (None if valid else "wiki_dependency_unavailable"),
            "created_at": candidate.created_at.isoformat()}


async def list_wiki(*, user_id, workspace_id, project_id=None):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=project_id is None)
        pages = list((await db.scalars(select(MemoryWikiPage).where(*scope.predicates(MemoryWikiPage))
            .order_by(MemoryWikiPage.updated_at.desc()).limit(100))).all())
        candidates = list((await db.scalars(select(MemoryWikiCandidate).where(*scope.predicates(MemoryWikiCandidate))
            .order_by(MemoryWikiCandidate.created_at.desc()).limit(100))).all())
        # Global management scope is only for finding rows. A page's original
        # selected project still fixes which source domains may contribute.
        page_views, candidate_views = [], []
        for page in pages:
            local = await resolve_access_scope(db, user_id=user_id, workspace_id=scope.workspace_id, project_id=page.project_id)
            page_views.append(await _page_view(db, local, page))
        for candidate in candidates:
            local = await resolve_access_scope(db, user_id=user_id, workspace_id=scope.workspace_id, project_id=candidate.project_id)
            candidate_views.append(await _candidate_view(db, local, candidate))
        return {"pages": page_views, "candidates": candidate_views}


async def get_job(*, user_id, workspace_id, job_id):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        job = await db.scalar(select(MemoryWikiJob).where(MemoryWikiJob.id == job_id,
            MemoryWikiJob.user_id == user_id, MemoryWikiJob.workspace_id == scope.workspace_id))
        if job is None or job.project_id is not None and job.project_id not in scope.project_ids:
            return None
        return _job_view(job)


async def reject_candidate(*, user_id, workspace_id, candidate_id, candidate_revision, approved_hash):
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        candidate = await db.scalar(select(MemoryWikiCandidate).where(MemoryWikiCandidate.id == candidate_id,
            *scope.predicates(MemoryWikiCandidate)).with_for_update())
        if candidate is None:
            return None
        if candidate.revision != candidate_revision or candidate.candidate_hash != approved_hash or candidate.status != "PENDING":
            raise WikiStateError("wiki_candidate_changed")
        candidate.status, candidate.decided_at = "REJECTED", now()
        return {"id": candidate.id, "status": "rejected"}
