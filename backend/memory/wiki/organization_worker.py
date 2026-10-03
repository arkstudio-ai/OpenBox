"""Resumable concept extraction and page scheduling, one durable step per lease."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
import uuid

from sqlalchemy import and_, or_, select, update

from core import config as runtime_config
from core.log import create_logger
from db.base import get_db_session
from db.models.memory_wiki import MemoryWikiJob
from db.models.wiki_platform import WikiConcept, WikiConceptBinding, WikiConceptExtraction, WikiOrganizationRun
from memory.policy import MemoryAccessDenied, resolve_access_scope
from memory.providers.common import MemoryProviderError
from memory.redaction import redact_value
from memory.service import lock_memory_authority
from memory.wiki import organization as service
from memory.wiki.concept_provider import ConfiguredConceptModel
from memory.wiki.organization_apply import apply_extractions
from memory.wiki.service import WikiStateError, now, read_sources, schedule_compile
from wiki_compiler.contracts import SourceSnapshot
from wiki_compiler.organization import OrganizationError, OrganizationRequest, validate_concepts

log = create_logger("memory.wiki.organization")
BUDGET_CODES = {"wiki_organization_budget_exhausted", "wiki_maintenance_budget_exhausted"}


@dataclass(frozen=True)
class OrganizationLease:
    id: str
    user_id: str
    owner: str
    generation: int


def claimable(instant):
    return or_(and_(WikiOrganizationRun.status.in_(["PENDING", "RETRY"]), WikiOrganizationRun.available_at <= instant),
               and_(WikiOrganizationRun.status == "RUNNING", WikiOrganizationRun.lease_until < instant))


async def claim(owner, config):
    if not config.wiki:
        return None
    async with get_db_session() as db:
        exhausted = update(WikiOrganizationRun).where(claimable(now()), WikiOrganizationRun.attempts >= config.max_attempts)
        if config.allowed_user_ids:
            exhausted = exhausted.where(WikiOrganizationRun.user_id.in_(config.allowed_user_ids))
        await db.execute(exhausted.values(status="FAILED", reason_code="wiki_organization_attempts_exhausted",
                                         lease_until=None, updated_at=now()))
        stmt = select(WikiOrganizationRun).where(claimable(now()), WikiOrganizationRun.attempts < config.max_attempts)
        if config.allowed_user_ids:
            stmt = stmt.where(WikiOrganizationRun.user_id.in_(config.allowed_user_ids))
        row = await db.scalar(stmt.order_by(WikiOrganizationRun.available_at, WikiOrganizationRun.id).limit(1))
        if row is None:
            return None
        generation = row.lease_generation + 1
        result = await db.execute(update(WikiOrganizationRun).where(WikiOrganizationRun.id == row.id,
            WikiOrganizationRun.lease_generation == row.lease_generation, claimable(now())).values(
            status="RUNNING", lease_owner=owner, lease_generation=generation,
            lease_until=now() + timedelta(seconds=max(config.worker_lease_seconds, config.compilation_timeout_seconds + 30)),
            attempts=WikiOrganizationRun.attempts + 1).execution_options(synchronize_session=False))
        return OrganizationLease(row.id, row.user_id, owner, generation) if result.rowcount == 1 else None


async def live(db, lease, *, lock=False):
    stmt = select(WikiOrganizationRun).where(WikiOrganizationRun.id == lease.id,
        WikiOrganizationRun.status == "RUNNING", WikiOrganizationRun.lease_owner == lease.owner,
        WikiOrganizationRun.lease_generation == lease.generation, WikiOrganizationRun.lease_until > now())
    row = await db.scalar(stmt.with_for_update() if lock else stmt)
    if row is None:
        raise WikiStateError("wiki_organization_lease_lost")
    return row


async def validated_scope(db, run, config):
    service.require_enabled(config, run.user_id)
    scope = await resolve_access_scope(db, user_id=run.user_id, workspace_id=run.workspace_id, project_id=run.project_id)
    if (await service.inventory(db, scope, config))["input_hash"] != run.input_hash:
        raise WikiStateError("wiki_organization_inputs_changed")
    return scope


def release(run, *, delay=0):
    run.status, run.lease_until, run.attempts, run.reason_code = "PENDING", None, 0, None
    run.available_at, run.updated_at, run.revision = now() + timedelta(seconds=delay), now(), run.revision + 1


async def _extract(lease, config, model):
    async with get_db_session() as db:
        run = await live(db, lease)
        scope = await validated_scope(db, run, config)
        cursor = run.cursor
        if cursor >= len(run.spec["records"]):
            request = None
            cached = None
        else:
            record = run.spec["records"][cursor]
            cached = await service.cached_extraction(db, scope, record)
            sources = await read_sources(db, scope, record["source_manifest"], record["memory_manifest"], acl_epoch=scope.acl_epoch)
            request = OrganizationRequest(record["memory_id"], tuple(SourceSnapshot(source.id, source.source_revision,
                source.body, source.content_hash, run.domain, scope.acl_epoch) for source in sources.values()),
                tuple(run.spec["catalog"]), run.spec["model"])
    if request and not cached:
        await service.reserve_call(lease.id, generation=lease.generation, owner=lease.owner)
        output, usage = await asyncio.wait_for(model.extract(request), timeout=config.compilation_timeout_seconds + 5)
        concepts = validate_concepts(output, request)
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=lease.user_id)
        run = await live(db, lease, lock=True)
        scope = await validated_scope(db, run, config)
        if run.cursor != cursor:
            raise WikiStateError("wiki_organization_lease_lost")
        result = dict(run.result)
        if request:
            record = run.spec["records"][cursor]
            row = await service.cached_extraction(db, scope, record)
            if row is None:
                if cached is not None:
                    raise WikiStateError("wiki_organization_inputs_changed")
                row = await db.scalar(select(WikiConceptExtraction).where(WikiConceptExtraction.cache_key == record["cache_key"]))
                if row is None:
                    row = WikiConceptExtraction(id="wiki_extract_" + record["cache_key"][:40], **service.scoped_values(scope),
                        domain=run.domain, cache_key=record["cache_key"], memory_id=record["memory_id"],
                        memory_revision=record["memory_manifest"][0]["revision"], source_manifest=record["source_manifest"],
                        memory_manifest=record["memory_manifest"], acl_epoch=scope.acl_epoch, model=request.model,
                        prompt_version=request.prompt_version)
                    db.add(row)
                row.concepts, row.usage, row.status = concepts, redact_value(usage), "CURRENT"
            else:
                result["reused_count"] = result.get("reused_count", 0) + 1
            result["extraction_ids"] = [*result["extraction_ids"], row.id]
            run.cursor += 1
        if run.cursor >= len(run.spec["records"]):
            result["phase"] = "reconciling"
        run.result = result
        release(run)


async def _reconcile(lease, config):
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=lease.user_id)
        run = await live(db, lease, lock=True)
        scope = await validated_scope(db, run, config)
        concept_ids = await apply_extractions(db, scope, run)
        if len(concept_ids) > config.wiki_organization_max_concepts:
            raise WikiStateError("wiki_organization_concept_limit")
        run.result = {**run.result, "concept_ids": concept_ids, "concept_count": len(concept_ids),
                      "page_cursor": 0, "phase": "scheduling" if run.spec["compile_pages"] else "complete"}
        if config.automatic_knowledge and run.spec["compile_pages"]:
            from memory.wiki.consolidation import adopt_legacy_pages
            await adopt_legacy_pages(db, scope, run)
            catalog = await service.identity_catalog(db, scope)
            run.result = {**run.result, "phase": "consolidating", "consolidation_cursor": 0,
                "consolidation_ids": [item.id for item in sorted(catalog,
                    key=lambda item: (item.category, item.canonical_key)) if item.status != "MERGED"]}
        release(run)
        if not run.spec["compile_pages"]:
            run.status = "PARTIAL" if run.spec["skipped"] else "COMPLETED"


async def _automatic_batches(db, scope, memory_ids):
    """Split by actual source and fallback size, never ask consumers to select sources."""
    from db.models.memory import UserMemory
    from memory.wiki.service import collect_compile_sources
    groups, current, fallback_size = [], [], 0
    for memory_id in memory_ids:
        memory = await db.get(UserMemory, memory_id)
        sources, refs = await collect_compile_sources(db, scope, [memory_id])
        size = len(memory.value["summary"]) + sum(len(key) + 24 for key in refs[0]["source_ids"]) + 8
        fits = len(current) < 12 and fallback_size + size <= 7400
        if fits and current:
            try:
                await collect_compile_sources(db, scope, [*current, memory_id])
            except WikiStateError as exc:
                if exc.code != "wiki_source_budget_exceeded":
                    raise
                fits = False
        if current and not fits:
            groups.append(current)
            current, fallback_size = [], 0
        current.append(memory_id)
        fallback_size += size
    return [*groups, current] if current else groups


async def _schedule(lease, config):
    from memory.wiki.service import _target, target_identity
    async with get_db_session() as db:
        run = await live(db, lease)
        scope = await validated_scope(db, run, config)
        cursor = run.result["page_cursor"]
        ids = run.result["concept_ids"]
        arguments, skipped = [], None
        if cursor < len(ids):
            concept = await db.get(WikiConcept, ids[cursor])
            bindings = await service.current_bindings(db, scope, concept)
            if not bindings:
                raise WikiStateError("wiki_organization_inputs_changed")
            memory_ids = sorted({binding.memory_id for binding in bindings})
            groups = await _automatic_batches(db, scope, memory_ids) if config.automatic_knowledge else [memory_ids]
            if (config.automatic_knowledge and not concept.user_edited
                    and len(memory_ids) < config.wiki_min_topic_memories):
                # The single fact is already in the memory list; a page would
                # only repeat it, and be recalled twice beside it.
                groups = []
                skipped = {"concept_id": concept.id, "page_id": concept.page_id, "status": "skipped_low_support"}
            for index, group in enumerate(groups):
                slug = concept.slug if not index else concept.slug[:65] + "-part-" + str(index + 1)
                title = concept.title if not index else concept.title[:150] + " · " + str(index + 1)
                identity = target_identity(scope, slug, scope.project_id)
                target = await _target(db, scope, identity)
                if config.automatic_knowledge and target:
                    title = target.title  # A simple user title edit survives later organization.
                arguments.append(({"user_id": run.user_id, "workspace_id": run.workspace_id, "project_id": run.project_id,
                    "slug": slug, "title": title, "memory_ids": group,
                    "request_id": "org:" + run.id + ":" + concept.id + (":" + str(index) if index else ""),
                    "organization_id": run.id, "config": config}, "wiki_" + identity[:40]))
    pages = [skipped] if not arguments and cursor < len(ids) and skipped else []
    for args, page_id in arguments:
        try:
            queued = await schedule_compile(**args)
            pages.append({"concept_id": concept.id, "job_id": queued.get("id"), "page_id": page_id,
                          "status": queued["status"]})
        except WikiStateError as exc:
            if exc.code != "wiki_source_budget_exceeded":
                raise
            pages.append({"concept_id": concept.id, "page_id": page_id,
                "status": "unavailable" if config.automatic_knowledge else "needs_selection", "reason_code": exc.code})
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=lease.user_id)
        run = await live(db, lease, lock=True)
        await validated_scope(db, run, config)
        result = dict(run.result)
        if pages:
            result["pages"] = [*result["pages"], *pages]
            result["page_cursor"] = cursor + 1
        if result["page_cursor"] >= len(ids):
            result["phase"] = "compiling"
        run.result = result
        release(run)


async def _observe_pages(lease, config):
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=lease.user_id)
        run = await live(db, lease, lock=True)
        await validated_scope(db, run, config)
        pages = [dict(item) for item in run.result["pages"]]
        waiting, paused, failed = False, False, bool(run.spec["skipped"])
        for page in pages:
            job = await db.get(MemoryWikiJob, page.get("job_id")) if page.get("job_id") else None
            if job:
                page["status"], page["candidate_id"], page["reason_code"] = job.status.lower(), job.candidate_id, job.last_error
                waiting |= job.status in {"PENDING", "RUNNING", "RETRY"}
                paused |= job.status == "PAUSED"
                failed |= job.status in {"FAILED", "CANCELLED"}
            elif page["status"] not in {"unchanged", "skipped_low_support"}:
                failed = True
        run.result = {**run.result, "pages": pages}
        release(run, delay=config.worker_interval_seconds)
        if paused:
            run.status, run.reason_code = "PAUSED", "wiki_organization_budget_exhausted"
        elif not waiting:
            run.status = "PARTIAL" if failed else "COMPLETED"
            run.result = {**run.result, "phase": "complete"}


async def fail(lease, code, config, *, terminal=False):
    async with get_db_session() as db:
        try:
            run = await live(db, lease, lock=True)
        except WikiStateError:
            return
        if code in BUDGET_CODES:
            status = "PAUSED"
        elif terminal:
            status = "CANCELLED"
        elif run.attempts >= config.max_attempts:
            status = "FAILED"
        else:
            status = "RETRY"
        run.status, run.reason_code, run.lease_until = status, code, None
        run.available_at, run.updated_at = now() + timedelta(seconds=min(60, 2 ** run.attempts)), now()
        run.revision += 1


class WikiOrganizationWorker:
    def __init__(self, config=None, *, model=None, consolidator=None, verifier=None):
        self.config, self.model = config, model
        self.consolidator, self.verifier = consolidator, verifier
        self.owner = "wiki-organization:" + uuid.uuid4().hex
        self._task, self._stop, self._lock = None, asyncio.Event(), asyncio.Lock()

    def _config(self):
        return self.config or runtime_config.get_config().memory

    def start(self):
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="wiki-organization")

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_once(self):
        if self._lock.locked():
            return False
        async with self._lock:
            config = self._config()
            lease = await claim(self.owner, config)
            if lease is None:
                return False
            try:
                async with get_db_session() as db:
                    phase = (await live(db, lease)).result["phase"]
                if phase == "extracting":
                    await _extract(lease, config, self.model or ConfiguredConceptModel(config))
                elif phase == "reconciling":
                    await _reconcile(lease, config)
                elif phase == "consolidating":
                    from memory.wiki.consolidation import step
                    await asyncio.wait_for(step(lease, config, model=self.consolidator, verifier=self.verifier),
                        timeout=config.compilation_timeout_seconds + 5)
                elif phase == "scheduling":
                    await _schedule(lease, config)
                elif phase == "compiling":
                    await _observe_pages(lease, config)
                else:
                    raise WikiStateError("wiki_organization_invalid_phase")
            except (WikiStateError, MemoryAccessDenied) as exc:
                await fail(lease, exc.code if isinstance(exc, WikiStateError) else "wiki_policy_denied", config, terminal=True)
            except (OrganizationError, MemoryProviderError, asyncio.TimeoutError) as exc:
                code = exc.code if isinstance(exc, MemoryProviderError) else str(exc) if isinstance(exc, OrganizationError) else "wiki_provider_timeout"
                await fail(lease, code, config)
            except asyncio.CancelledError:
                await fail(lease, "wiki_worker_stopped", config)
                raise
            return True

    async def _loop(self):
        next_scan = 0.0
        while not self._stop.is_set():
            try:
                from memory.wiki.maintenance import schedule_due
                instant = asyncio.get_running_loop().time()
                if instant >= next_scan:
                    await schedule_due(self._config())
                    next_scan = instant + self._config().wiki_auto_scan_seconds
                if await self.run_once():
                    await asyncio.sleep(0)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Wiki organization recovery pending error_type=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._config().worker_interval_seconds)
            except asyncio.TimeoutError:
                pass
