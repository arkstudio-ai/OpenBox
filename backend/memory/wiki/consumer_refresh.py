"""Resume legacy drafts and refresh corrected articles without consumer actions."""
from sqlalchemy import select

from core.identifier import ascending
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_wiki import MemoryWikiCandidate, MemoryWikiJob, MemoryWikiPage
from db.models.wiki_platform import WikiConcept, WikiMaintenancePolicy
from memory.policy import MemoryAccessDenied, active_memory_predicates, resolve_access_scope
from memory.service import lock_memory_authority
from memory.wiki.service import WikiStateError, _candidate_intact, dependencies_current, now, schedule_compile
from wiki_compiler.hashing import canonical_hash


async def refresh_existing(policy_id, config):
    refresh = []
    async with get_db_session() as db:
        policy = await db.get(WikiMaintenancePolicy, policy_id)
        if not policy or not policy.enabled or not policy.automatic or not config.enabled("wiki", policy.user_id):
            return
        await lock_memory_authority(db, user_id=policy.user_id)
        try:
            scope = await resolve_access_scope(db, user_id=policy.user_id, workspace_id=policy.workspace_id,
                                              project_id=policy.project_id)
        except MemoryAccessDenied:
            return
        identity = {"user_id": policy.user_id, "workspace_id": policy.workspace_id, "project_id": policy.project_id}
        candidates = (await db.scalars(select(MemoryWikiCandidate).where(*scope.predicates(MemoryWikiCandidate),
            MemoryWikiCandidate.project_id == policy.project_id, MemoryWikiCandidate.status == "PENDING")
            .order_by(MemoryWikiCandidate.created_at).limit(20))).all()
        for candidate in candidates:
            old = await db.get(MemoryWikiJob, candidate.job_id)
            if not old or old.status != "COMPLETED":
                continue
            # Preserve old candidates as audit history. Their summaries must
            # pass the new independent check, even if compilation is cached.
            candidate.status, candidate.reason_code = "SUPERSEDED", "automatic_revalidation"
            if not _candidate_intact(candidate) or not await dependencies_current(db, scope, candidate):
                continue
            spec = {key: value for key, value in old.spec.items() if key != "organization_id"}
            spec["maintenance_id"] = policy.id
            instant = now()
            db.add(MemoryWikiJob(id=ascending("wiki_job"), **identity,
                target_identity=old.target_identity, input_hash=old.input_hash,
                request_id=f"automatic-revalidate:{candidate.id}", spec=spec, status="PENDING",
                attempts=0, lease_generation=0, available_at=instant, created_at=instant, updated_at=instant))
        jobs = (await db.scalars(select(MemoryWikiJob).where(*scope.predicates(MemoryWikiJob),
            MemoryWikiJob.project_id == policy.project_id, MemoryWikiJob.status == "PAUSED"))).all()
        for job in jobs:
            if job.spec.get("maintenance_id") == policy.id and policy.calls_used < policy.call_limit:
                job.status, job.available_at, job.attempts = "PENDING", now(), 0
        pages = (await db.scalars(select(MemoryWikiPage).where(*scope.predicates(MemoryWikiPage),
            MemoryWikiPage.project_id == policy.project_id, MemoryWikiPage.status.in_(["STALE", "PUBLISHED"]),
            MemoryWikiPage.deleted_at.is_(None)).order_by(MemoryWikiPage.updated_at).limit(40))).all()
        for page in pages:
            ids = [item["id"] for item in page.memory_manifest if item["kind"] == "memory"]
            concept = await db.scalar(select(WikiConcept).where(*scope.predicates(WikiConcept),
                WikiConcept.page_id == page.id, WikiConcept.status == "ACTIVE"))
            if ids and await _retire_if_unsupported(db, scope, config, page, concept, ids):
                continue
            merged_needs_refresh = False
            if concept and concept.extra_metadata.get("consolidations"):
                from memory.wiki.organization import current_bindings
                from memory.wiki.automatic import COMPLETE_TOPIC_POLICY
                bindings = await current_bindings(db, scope, concept)
                if not bindings:
                    continue
                current_ids = sorted({binding.memory_id for binding in bindings})
                # Larger growing topics are partitioned by the organizer.
                # Do not republish a compact merged article using its old subset.
                if len(current_ids) > 12:
                    continue
                merged_needs_refresh = set(ids) != set(current_ids) or page.policy_version != COMPLETE_TOPIC_POLICY
                ids = current_ids
            if page.status == "PUBLISHED":
                candidate = await db.get(MemoryWikiCandidate, page.candidate_id)
                if not merged_needs_refresh and (not candidate or candidate.usage.get("automatic_grounding", {}).get("mode") != "verbatim_sources"):
                    continue
            if not ids:
                continue
            memories = (await db.scalars(select(UserMemory).where(*scope.predicates(UserMemory),
                *active_memory_predicates(), UserMemory.id.in_(ids)))).all()
            # Missing/forgotten material must not be silently restored or
            # borrowed from another scope. Remaining evidence can still stand.
            if memories:
                fingerprint = canonical_hash(sorted((row.id, row.revision) for row in memories))[:24]
                refresh.append({**identity, "slug": page.slug, "title": page.title,
                    "memory_ids": [row.id for row in memories],
                    "expected_page_revision": page.revision,
                    "request_id": f"automatic-refresh:{page.id}:{page.revision}:{fingerprint}"})
    for item in refresh:
        try:
            await schedule_compile(**item, config=config, maintenance_id=policy_id)
        except (WikiStateError, MemoryAccessDenied):
            # Authority changed between scans. A future current snapshot wins.
            continue


async def _retire_if_unsupported(db, scope, config, page, concept, manifest_ids) -> bool:
    """Retire a memory-built page with nothing left to stand on.

    No admitted memory left: there is nothing to rebuild from, and the page
    would otherwise wait as "updating" forever. Too little support: only for
    organizer topics that automation published and nobody renamed. Neither
    case deletes text.
    """
    from memory.wiki.organization import current_bindings
    from memory.wiki.retirement import LOW_SUPPORT, NO_SOURCES, automatically_published, retire_page
    active = set((await db.scalars(select(UserMemory.id).where(*scope.predicates(UserMemory),
        *active_memory_predicates(), UserMemory.id.in_(manifest_ids)))).all())
    if not active and page.status == "STALE":
        return await retire_page(db, page, config, NO_SOURCES)
    # Only organizer topics: a page made any other way may carry a person's
    # title or curation that nothing records, so it is never retired for size.
    if not config.automatic_knowledge or concept is None or concept.user_edited:
        return False
    support = {binding.memory_id for binding in await current_bindings(db, scope, concept)} or active
    if len(support) >= config.wiki_min_topic_memories or not await automatically_published(db, page):
        return False
    return await retire_page(db, page, config, LOW_SUPPORT)

