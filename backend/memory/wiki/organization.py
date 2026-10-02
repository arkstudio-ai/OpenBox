"""Scoped organization snapshots, incremental cache and explicit user actions."""
from datetime import timedelta

from sqlalchemy import select

from core import config as runtime_config
from core.identifier import ascending
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_wiki import MemoryWikiJob, MemoryWikiPage
from db.models.wiki_platform import (
    WikiConcept, WikiConceptBinding, WikiConceptExtraction, WikiMaintenancePolicy,
    WikiOrganizationRun, WikiRelation,
)
from memory.policy import active_memory_predicates, resolve_access_scope
from memory.service import lock_memory_authority
from memory.wiki.service import (
    WikiStateError, collect_compile_sources, dependencies_current, domain_for,
    now, read_sources, _source_ref, _page_view,
)
from wiki_compiler.hashing import canonical_hash
from wiki_compiler.organization import ORGANIZATION_VERSION, normalize_name


def scoped_values(scope):
    instant = now()
    return {"user_id": scope.user_id, "workspace_id": scope.workspace_id, "project_id": scope.project_id,
            "visibility": "PERSONAL", "revision": 1, "created_at": instant, "updated_at": instant}


def require_enabled(config, user_id):
    if not config.enabled("wiki", user_id):
        raise WikiStateError("wiki_disabled")


async def inventory(db, scope, config):
    """Complete bounded inventory, including explicit records of unusable inputs."""
    rows = list((await db.scalars(select(UserMemory).where(*scope.predicates(UserMemory),
        *active_memory_predicates()).order_by(UserMemory.id).limit(config.wiki_organization_max_memories + 1))).all())
    if len(rows) > config.wiki_organization_max_memories:
        raise WikiStateError("wiki_organization_memory_limit")
    model = config.extract_model or runtime_config.get_config().model
    records, skipped = [], []
    for memory in rows:
        try:
            sources, memories = await collect_compile_sources(db, scope, [memory.id])
        except WikiStateError as exc:
            skipped.append({"memory_id": memory.id, "revision": memory.revision, "reason_code": exc.code})
            continue
        source_manifest = [_source_ref(source, scope) for source in sorted(sources.values(), key=lambda item: item.id)]
        record = {"memory_id": memory.id, "memory_manifest": memories, "source_manifest": source_manifest,
                  "acl_epoch": scope.acl_epoch, "model": model, "prompt_version": ORGANIZATION_VERSION}
        record["cache_key"] = canonical_hash({"domain": domain_for(scope, scope.project_id), **record})
        records.append(record)
    from memory.wiki.consolidation import POLICY as CONSOLIDATION_POLICY
    digest = canonical_hash({"records": records, "skipped": skipped,
        "consolidation": CONSOLIDATION_POLICY if config.automatic_knowledge else None})
    return {"records": records, "skipped": skipped, "input_hash": digest, "model": model}


async def cached_extraction(db, scope, record):
    row = await db.scalar(select(WikiConceptExtraction).where(*scope.predicates(WikiConceptExtraction),
        WikiConceptExtraction.cache_key == record["cache_key"], WikiConceptExtraction.status == "CURRENT"))
    return row if row is not None and await dependencies_current(db, scope, row) else None


async def identity_catalog(db, scope):
    return list((await db.scalars(select(WikiConcept).where(*scope.predicates(WikiConcept),
        WikiConcept.domain == domain_for(scope, scope.project_id)).order_by(WikiConcept.canonical_key))).all())


def catalog_entry(concept):
    return {"id": concept.id, "canonical_key": concept.canonical_key, "title": concept.title,
            "aliases": concept.aliases, "category": concept.category}


async def current_bindings(db, scope, concept):
    bindings = list((await db.scalars(select(WikiConceptBinding).where(
        WikiConceptBinding.concept_id == concept.id, WikiConceptBinding.status == "CURRENT"))).all())
    if concept.status != "ACTIVE" or not bindings:
        return []
    for binding in bindings:
        extraction = await db.get(WikiConceptExtraction, binding.extraction_id)
        if extraction is None or extraction.status != "CURRENT" or not await dependencies_current(db, scope, extraction):
            return []
    return bindings


async def model_catalog(db, scope, config):
    concepts = await identity_catalog(db, scope)
    if len(concepts) > config.wiki_organization_max_concepts:
        raise WikiStateError("wiki_organization_concept_limit")
    result = []
    for concept in concepts:
        if concept.status == "MERGED":
            continue
        # A correction invalidates evidence, not the identity of its topic.
        # Reuse names only while at least one originally bound memory is still
        # admitted and readable. Forgotten/foreign material never seeds a model.
        bound_ids = select(WikiConceptBinding.memory_id).where(WikiConceptBinding.concept_id == concept.id)
        memories = (await db.scalars(select(UserMemory).where(*scope.predicates(UserMemory),
            *active_memory_predicates(), UserMemory.id.in_(bound_ids)))).all()
        from memory.service import memory_sources_available
        if any([await memory_sources_available(db, scope, memory) for memory in memories]):
            result.append(catalog_entry(concept))
    return result


async def preview(*, user_id, workspace_id, project_id=None, config=None):
    config = config or runtime_config.get_config().memory
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        snapshot = await inventory(db, scope, config)
        reusable = sum([bool(await cached_extraction(db, scope, item)) for item in snapshot["records"]])
        concepts = await identity_catalog(db, scope)
        return {"input_hash": snapshot["input_hash"], "memory_count": len(snapshot["records"]),
                "changed_count": len(snapshot["records"]) - reusable, "reused_count": reusable,
                "concept_count": len(concepts), "skipped": snapshot["skipped"], "model": snapshot["model"],
                "publication_requires_approval": True, "max_model_calls": config.wiki_organization_max_calls}


def run_view(run):
    # Progress contains identities, counts and reason codes, never old quotes.
    return {"id": run.id, "revision": run.revision, "project_id": run.project_id,
            "status": run.status.lower(), "phase": run.result.get("phase", "extracting"),
            "memory_count": len(run.spec["records"]), "processed": run.cursor,
            "model_calls": run.model_calls, "max_model_calls": run.spec["max_model_calls"],
            "reused_count": run.result.get("reused_count", 0),
            "pages": run.result.get("pages", []), "concept_count": run.result.get("concept_count", 0),
            "skipped": run.spec.get("skipped", []), "reason_code": run.reason_code,
            "can_resume": run.status in {"PAUSED", "FAILED"} or run.status == "PARTIAL" and any(
                page.get("job_id") and page.get("status") == "failed" for page in run.result.get("pages", [])),
            "created_at": run.created_at.isoformat(), "updated_at": run.updated_at.isoformat()}


async def enqueue(db, scope, snapshot, *, request_id, max_model_calls, compile_pages, config, automation_id=None):
    require_enabled(config, scope.user_id)
    if not 1 <= max_model_calls <= config.wiki_organization_max_calls:
        raise WikiStateError("wiki_organization_invalid_budget")
    previous = await db.scalar(select(WikiOrganizationRun).where(WikiOrganizationRun.user_id == scope.user_id,
        WikiOrganizationRun.workspace_id == scope.workspace_id, WikiOrganizationRun.request_id == request_id))
    if previous:
        if (previous.project_id != scope.project_id or previous.input_hash != snapshot["input_hash"]
                or previous.spec["max_model_calls"] != max_model_calls or previous.spec["compile_pages"] != compile_pages):
            raise WikiStateError("wiki_request_conflict")
        return previous
    catalog = await model_catalog(db, scope, config)
    run = WikiOrganizationRun(id=ascending("wiki_org"), **scoped_values(scope), domain=domain_for(scope, scope.project_id),
        request_id=request_id, input_hash=snapshot["input_hash"],
        spec={**snapshot, "catalog": catalog, "max_model_calls": max_model_calls, "compile_pages": compile_pages},
        result={"phase": "extracting", "reused_count": 0, "extraction_ids": [], "pages": []},
        status="PENDING", cursor=0, model_calls=0, attempts=0, lease_generation=0,
        available_at=now(), automation_id=automation_id)
    db.add(run)
    await db.flush()
    return run


async def start(*, user_id, workspace_id, project_id, input_hash, request_id,
                max_model_calls, compile_pages=True, config=None):
    config = config or runtime_config.get_config().memory
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        snapshot = await inventory(db, scope, config)
        if snapshot["input_hash"] != input_hash:
            raise WikiStateError("wiki_organization_preview_changed")
        return run_view(await enqueue(db, scope, snapshot, request_id=request_id,
            max_model_calls=max_model_calls, compile_pages=compile_pages, config=config))


async def runs(*, user_id, workspace_id, project_id=None, run_id=None, offset=0):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=project_id is None)
        stmt = select(WikiOrganizationRun).where(*scope.predicates(WikiOrganizationRun))
        if run_id:
            row = await db.scalar(stmt.where(WikiOrganizationRun.id == run_id))
            return run_view(row) if row else None
        rows = list((await db.scalars(stmt.order_by(WikiOrganizationRun.created_at.desc(), WikiOrganizationRun.id)
            .offset(offset).limit(41))).all())
        return {"runs": [run_view(row) for row in rows[:40]], "next_offset": offset + 40 if len(rows) > 40 else None}


async def concepts(*, user_id, workspace_id, project_id=None, offset=0, query="", category=None):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=project_id is None)
        rows = list((await db.scalars(select(WikiConcept).where(*scope.predicates(WikiConcept),
            WikiConcept.status != "MERGED").order_by(WikiConcept.updated_at.desc(), WikiConcept.id)
            .offset(offset).limit(200))).all())
        result, consumed = [], 0
        for concept in rows:
            consumed += 1
            local = await resolve_access_scope(db, user_id=user_id, workspace_id=scope.workspace_id, project_id=concept.project_id)
            bindings = await current_bindings(db, local, concept)
            valid = bool(bindings)
            text = " ".join([concept.title, *concept.aliases, concept.description]) if valid else ""
            if query.casefold() not in text.casefold() or category and (not valid or category != concept.category):
                continue
            result.append({"id": concept.id, "revision": concept.revision, "project_id": concept.project_id,
                "title": concept.title if valid else None, "aliases": concept.aliases if valid else [],
                "category": concept.category if valid else None, "description": concept.description if valid else None,
                "status": concept.status.lower() if valid else "stale", "page_id": concept.page_id,
                "page_available": bool(valid and await published_concept_page(db, local, concept)),
                "slug": concept.slug, "memory_ids": [binding.memory_id for binding in bindings],
                "evidence": [item for binding in bindings for item in binding.evidence], "body_available": valid})
            if len(result) == 40:
                break
        return {"concepts": result, "next_offset": offset + consumed if consumed < len(rows) or len(rows) == 200 else None}


async def published_concept_page(db, scope, concept):
    if not concept or not concept.page_id:
        return None
    page = await db.scalar(select(MemoryWikiPage).where(MemoryWikiPage.id == concept.page_id, *scope.predicates(MemoryWikiPage)))
    return page.id if page and (await _page_view(db, scope, page))["body_available"] else None


async def act_run(*, user_id, workspace_id, run_id, expected_revision, action, max_model_calls=None):
    config = runtime_config.get_config().memory
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        run = await db.scalar(select(WikiOrganizationRun).where(WikiOrganizationRun.id == run_id,
            *scope.predicates(WikiOrganizationRun)).with_for_update())
        if run is None:
            return None
        if run.revision != expected_revision:
            raise WikiStateError("wiki_organization_changed")
        if action == "cancel" and run.status not in {"COMPLETED", "CANCELLED"}:
            run.status, run.reason_code = "CANCELLED", "cancelled_by_user"
            await cancel_page_jobs(db, run)
        elif action == "resume" and run.status in {"PAUSED", "FAILED", "PARTIAL"}:
            if not run_view(run)["can_resume"]:
                raise WikiStateError("wiki_organization_selection_required")
            require_enabled(config, user_id)
            if max_model_calls is None or not run.model_calls < max_model_calls <= config.wiki_organization_max_calls:
                raise WikiStateError("wiki_organization_invalid_budget")
            local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=run.project_id)
            if (await inventory(db, local, config))["input_hash"] != run.input_hash:
                raise WikiStateError("wiki_organization_inputs_changed")
            run.spec = {**run.spec, "max_model_calls": max_model_calls}
            run.status, run.reason_code, run.attempts, run.available_at = "PENDING", None, 0, now()
            if run.result.get("phase") == "complete":
                run.result = {**run.result, "phase": "compiling"}
            for page in run.result.get("pages", []):
                job = await db.get(MemoryWikiJob, page.get("job_id")) if page.get("job_id") else None
                if job and job.spec.get("organization_id") == run.id and job.status in {"PAUSED", "FAILED"}:
                    job.status, job.attempts, job.available_at = "PENDING", 0, now()
        else:
            raise WikiStateError("wiki_organization_invalid_action")
        run.lease_generation += 1
        run.lease_until = None
        run.revision += 1
        run.updated_at = now()
        return run_view(run)


async def cancel_page_jobs(db, run):
    for page in run.result.get("pages", []):
        job = await db.get(MemoryWikiJob, page.get("job_id")) if page.get("job_id") else None
        if job and job.spec.get("organization_id") == run.id and job.status in {"PENDING", "RETRY", "RUNNING", "PAUSED"}:
            job.status, job.last_error, job.updated_at = "CANCELLED", "wiki_organization_unavailable", now()
            job.lease_until, job.lease_generation = None, job.lease_generation + 1


async def reserve_call(run_id, *, generation=None, owner=None):
    """Atomically count every actual provider attempt, including page retries."""
    config = runtime_config.get_config().memory
    async with get_db_session() as db:
        identity = await db.get(WikiOrganizationRun, run_id)
        if identity is None:
            raise WikiStateError("wiki_organization_unavailable")
        await lock_memory_authority(db, user_id=identity.user_id)
        run = await db.scalar(select(WikiOrganizationRun).where(WikiOrganizationRun.id == run_id).with_for_update())
        require_enabled(config, run.user_id)
        if run.status == "PAUSED" and run.reason_code in {
                "wiki_organization_budget_exhausted", "wiki_maintenance_budget_exhausted"}:
            raise WikiStateError(run.reason_code)
        if run.status not in {"PENDING", "RUNNING", "RETRY"}:
            raise WikiStateError("wiki_organization_unavailable")
        if generation is not None and (run.lease_generation != generation or run.lease_owner != owner
                                      or not run.lease_until or run.lease_until.replace(tzinfo=now().tzinfo) <= now()):
            raise WikiStateError("wiki_organization_lease_lost")
        scope = await resolve_access_scope(db, user_id=run.user_id, workspace_id=run.workspace_id, project_id=run.project_id)
        if (await inventory(db, scope, config))["input_hash"] != run.input_hash:
            raise WikiStateError("wiki_organization_inputs_changed")
        if run.model_calls >= run.spec["max_model_calls"]:
            raise WikiStateError("wiki_organization_budget_exhausted")
        if run.automation_id:
            policy = await db.scalar(select(WikiMaintenancePolicy).where(
                WikiMaintenancePolicy.id == run.automation_id).with_for_update())
            if policy is None or not policy.enabled:
                raise WikiStateError("wiki_maintenance_disabled")
            if policy.window_started_at.replace(tzinfo=now().tzinfo) + timedelta(hours=24) <= now():
                policy.window_started_at, policy.calls_used = now(), 0
            if policy.calls_used >= policy.call_limit:
                raise WikiStateError("wiki_maintenance_budget_exhausted")
            policy.calls_used += 1
        run.model_calls += 1
        run.revision += 1
        run.updated_at = now()


async def invalidate_concepts(db, *, memory_ids=(), source_ids=()):
    """Called in the same transaction as the existing derivative invalidation."""
    affected = set(memory_ids)
    if source_ids:
        from db.models.memory_v2 import MemorySourceLink
        affected.update((await db.scalars(select(MemorySourceLink.memory_id).where(
            MemorySourceLink.source_id.in_(source_ids)))).all())
    if not affected:
        return
    extractions = list((await db.scalars(select(WikiConceptExtraction).where(
        WikiConceptExtraction.memory_id.in_(affected), WikiConceptExtraction.status == "CURRENT"))).all())
    for row in extractions:
        row.status, row.concepts, row.updated_at = "STALE", [], now()
    bindings = list((await db.scalars(select(WikiConceptBinding).where(
        WikiConceptBinding.memory_id.in_(affected)))).all())
    for binding in bindings:
        binding.status, binding.evidence = "STALE", []
        concept = await db.get(WikiConcept, binding.concept_id)
        if concept and concept.status != "MERGED":
            concept.status, concept.updated_at = "STALE", now()
            concept.revision += 1
    relations = list((await db.scalars(select(WikiRelation).where(WikiRelation.status.in_(["ACTIVE", "PROPOSED"])))).all())
    for relation in relations:
        if affected & {item["id"] for item in relation.memory_manifest}:
            relation.status, relation.evidence, relation.updated_at = "STALE", [], now()
            relation.revision += 1


async def edit_concept(*, user_id, workspace_id, concept_id, expected_revision, title, aliases, category, description):
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        concept = await db.scalar(select(WikiConcept).where(WikiConcept.id == concept_id,
            *scope.predicates(WikiConcept)).with_for_update())
        if concept is None:
            return None
        if concept.revision != expected_revision or concept.status == "MERGED":
            raise WikiStateError("wiki_concept_changed")
        local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=concept.project_id)
        if not await current_bindings(db, local, concept):
            raise WikiStateError("wiki_source_changed")
        aliases = list(dict.fromkeys([*aliases, concept.title]))
        names = {normalize_name(name) for name in [title, *aliases]}
        for other in await identity_catalog(db, local):
            if other.id != concept.id and other.status != "MERGED" and names & {
                    normalize_name(name) for name in [other.title, *other.aliases]}:
                raise WikiStateError("wiki_concept_alias_conflict")
        concept.title, concept.aliases = title.strip(), [alias for alias in aliases if alias != title.strip()]
        concept.category, concept.description = category.strip(), description.strip()
        concept.user_edited, concept.revision, concept.updated_at = True, concept.revision + 1, now()
        from memory.wiki.maintenance import request_recheck
        await request_recheck(db, local)
        return {"id": concept.id, "revision": concept.revision}
