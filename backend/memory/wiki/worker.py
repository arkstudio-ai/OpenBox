"""Durable fenced compilation and atomic, grounded consumer publication."""
import asyncio
from dataclasses import asdict, dataclass
from datetime import timedelta
import uuid

from sqlalchemy import and_, or_, select, update

from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemoryDebugRun, MemoryDebugStep
from db.models.memory_wiki import MemoryWikiCandidate, MemoryWikiJob
from memory.policy import MemoryAccessDenied, resolve_access_scope
from memory.providers.common import MemoryProviderError
from memory.redaction import redact_value
from memory.service import lock_memory_authority
from memory.wiki.provider import ConfiguredWikiModel
from memory.wiki.service import (
    WikiStateError, _candidate_intact, _target, dependencies_current, immutable_candidate_hash, now, read_sources, target_is_deleted,
)
from wiki_compiler import CompilePolicy, CompileRequest, SourceSnapshot, TargetSnapshot, WikiContractError, compile_candidate
from wiki_compiler.hashing import canonical_hash, text_hash

log = create_logger("memory.wiki.worker")


@dataclass(frozen=True)
class WikiLease:
    id: str
    owner: str
    generation: int
    user_id: str
    workspace_id: str
    project_id: str | None


def _claimable(instant):
    return or_(and_(MemoryWikiJob.status.in_(["PENDING", "RETRY"]), MemoryWikiJob.available_at <= instant),
               and_(MemoryWikiJob.status == "RUNNING", MemoryWikiJob.lease_until < instant))


def _fence(lease):
    return (MemoryWikiJob.id == lease.id, MemoryWikiJob.status == "RUNNING", MemoryWikiJob.lease_owner == lease.owner,
            MemoryWikiJob.lease_generation == lease.generation, MemoryWikiJob.lease_until > now())


async def claim_job(owner, config):
    if not config.wiki:
        return None
    async with get_db_session() as db:
        instant = now()
        exhausted = update(MemoryWikiJob).where(_claimable(instant), MemoryWikiJob.attempts >= config.max_attempts)
        if config.allowed_user_ids:
            exhausted = exhausted.where(MemoryWikiJob.user_id.in_(config.allowed_user_ids))
        await db.execute(exhausted.values(status="FAILED", last_error="wiki_attempts_exhausted", lease_until=None, updated_at=instant))
        stmt = select(MemoryWikiJob).where(_claimable(instant), MemoryWikiJob.attempts < config.max_attempts)
        if config.allowed_user_ids:
            stmt = stmt.where(MemoryWikiJob.user_id.in_(config.allowed_user_ids))
        job = await db.scalar(stmt.order_by(MemoryWikiJob.created_at, MemoryWikiJob.id).limit(1))
        if job is None:
            return None
        generation = job.lease_generation + 1
        result = await db.execute(update(MemoryWikiJob).where(MemoryWikiJob.id == job.id,
            MemoryWikiJob.lease_generation == job.lease_generation, _claimable(instant)).values(
            status="RUNNING", lease_owner=owner, lease_generation=generation,
            lease_until=instant + timedelta(seconds=max(config.worker_lease_seconds, config.compilation_timeout_seconds * 2 + 30)),
            attempts=MemoryWikiJob.attempts + 1, updated_at=instant).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            return None
        return WikiLease(job.id, owner, generation, job.user_id, job.workspace_id, job.project_id)


async def read_request(lease, config):
    async with get_db_session() as db:
        job = await db.scalar(select(MemoryWikiJob).where(*_fence(lease)))
        if job is None or not config.enabled("wiki", lease.user_id):
            raise WikiStateError("wiki_lease_lost")
        await _organization_available(db, job)
        scope = await resolve_access_scope(db, user_id=lease.user_id, workspace_id=lease.workspace_id,
                                          project_id=lease.project_id)
        spec = job.spec
        sources = await read_sources(db, scope, spec["sources"], spec["memories"], acl_epoch=spec["acl_epoch"])
        target = await _target(db, scope, job.target_identity)
        expected = spec["target"]
        if ((target is None) != (expected["revision"] == 0)
                or target and (await target_is_deleted(db, target) or target.revision != expected["revision"] or target.content_hash != expected["content_hash"]
                               or target.body is not None and text_hash(target.body) != target.content_hash)):
            raise WikiStateError("wiki_target_changed")
        return CompileRequest(spec["slug"], spec["title"], spec["domain"],
            tuple(SourceSnapshot(source.id, source.source_revision, source.body, source.content_hash,
                                 spec["domain"], scope.acl_epoch) for source in sorted(sources.values(), key=lambda source: source.id)),
            TargetSnapshot(**expected), CompilePolicy(**spec["policy"]))


async def _organization_available(db, job):
    if not job.spec.get("organization_id"):
        return
    from db.models.wiki_platform import WikiOrganizationRun
    run = await db.get(WikiOrganizationRun, job.spec["organization_id"])
    if (run is None or run.user_id != job.user_id or run.workspace_id != job.workspace_id
            or run.project_id != job.project_id or run.status not in {"PENDING", "RUNNING", "RETRY", "PAUSED"}):
        raise WikiStateError("wiki_organization_unavailable")


async def admitted_fallback(lease):
    """Copy admitted facts, not surrounding chat instructions, on synthesis failure."""
    async with get_db_session() as db:
        job = await db.scalar(select(MemoryWikiJob).where(*_fence(lease)))
        if job is None:
            raise WikiStateError("wiki_lease_lost")
        scope = await resolve_access_scope(db, user_id=lease.user_id, workspace_id=lease.workspace_id,
                                          project_id=lease.project_id)
        sources = await read_sources(db, scope, job.spec["sources"], job.spec["memories"], acl_epoch=job.spec["acl_epoch"])
        paragraphs = []
        seen = set()
        for reference in job.spec["memories"]:
            memory = await db.get(UserMemory, reference["id"])
            summary = memory.value["summary"]
            if summary in seen:
                continue
            seen.add(summary)
            citations = []
            for source_id in reference["source_ids"]:
                body = sources[source_id].body
                exact = [item["quote"] for item in (memory.evidence or {}).get("quotes", [])
                         if isinstance(item, dict) and isinstance(item.get("quote"), str) and item["quote"] in body]
                quote = summary if summary in body else exact[0] if exact else body
                citations.append({"source_id": source_id, "quote": quote[:1600]})
            paragraphs.append({"text": summary, "citations": citations})
        return paragraphs


class OrganizationBudgetedModel:
    def __init__(self, model, organization_id):
        self.model, self.organization_id = model, organization_id

    @property
    def last_usage(self):
        return getattr(self.model, "last_usage", None)

    async def generate(self, request):
        await self.reserve()
        return await self.model.generate(request)

    async def reserve(self):
        from memory.wiki.organization import reserve_call
        await reserve_call(self.organization_id)


async def _budgeted_model(lease, model):
    async with get_db_session() as db:
        job = await db.scalar(select(MemoryWikiJob).where(*_fence(lease)))
        if job is None:
            raise WikiStateError("wiki_lease_lost")
        if job.spec.get("organization_id"):
            return OrganizationBudgetedModel(model, job.spec["organization_id"])
        if job.spec.get("maintenance_id"):
            return MaintenanceBudgetedModel(model, job.spec["maintenance_id"])
        return model


class MaintenanceBudgetedModel(OrganizationBudgetedModel):
    async def reserve(self):
        from core.config import get_config
        from memory.wiki.maintenance import reserve_policy_call
        await reserve_policy_call(self.organization_id, get_config().memory)


async def _pause_budget(lease, code):
    async with get_db_session() as db:
        job = await db.scalar(select(MemoryWikiJob).where(*_fence(lease)).with_for_update())
        if job:
            job.status, job.last_error, job.lease_until, job.updated_at = "PAUSED", code, None, now()
            job.attempts = max(0, job.attempts - 1)


class SQLCompilationCache:
    """Validated prior candidates form a durable cache; commit stores new output."""
    def __init__(self, lease):
        self.lease = lease

    async def get(self, key):
        async with get_db_session() as db:
            scope = await resolve_access_scope(db, user_id=self.lease.user_id, workspace_id=self.lease.workspace_id,
                                              project_id=self.lease.project_id)
            candidates = (await db.scalars(select(MemoryWikiCandidate).where(*scope.predicates(MemoryWikiCandidate))
                .order_by(MemoryWikiCandidate.created_at.desc()).limit(100))).all()
            for candidate in candidates:
                if (candidate.draft.get("cache_key") == key and _candidate_intact(candidate)
                        and await dependencies_current(db, scope, candidate)):
                    return {"paragraphs": [{"text": paragraph["text"], "citations": [
                        {"source_id": citation["source_id"], "quote": citation["quote"]}
                        for citation in paragraph["citations"]]} for paragraph in candidate.draft["paragraphs"]]}
        return None

    async def put(self, key, value):
        # No separate write: fenced candidate persistence is the cache commit.
        return None


async def _trace(db, job, config, *, status, reason_code, usage=None, candidate_id=None):
    if not config.enabled("debug_view", job.user_id):
        return
    identity = "wiki_trace_" + canonical_hash({"job": job.id, "generation": job.lease_generation})[:40]
    if await db.get(MemoryDebugRun, identity):
        return
    instant = now()
    sources = [{key: reference[key] for key in ("kind", "id", "revision", "content_hash")}
               for reference in job.spec["sources"]]
    run = MemoryDebugRun(id=identity, request_id=f"wiki:{job.id}", attempt_id=f"wiki:{job.id}:{job.lease_generation}",
        user_id=job.user_id, workspace_id=job.workspace_id, project_id=job.project_id,
        status=status.lower(), policy_version=config.policy_version, input_hash=job.input_hash,
        input_snapshot={"wiki_job_id": job.id, "source_count": len(sources), "content_retained": False},
        source_refs=sources, usage=redact_value(usage or {}), created_at=instant,
        expires_at=instant + timedelta(days=config.debug_retention_days))
    db.add(run)
    await db.flush()
    db.add(MemoryDebugStep(id=ascending("memory_step"), run_id=run.id, phase="wiki", status=status.lower(),
        reason_code=reason_code, data={"job_id": job.id, "candidate_id": candidate_id,
            "attempts": job.attempts, "lease_generation": job.lease_generation,
            "source_refs": sources, "expected_target": job.spec["target"],
            "candidate_status": ("approved" if config.automatic_knowledge else "pending") if candidate_id else None,
            "published": bool(candidate_id and config.automatic_knowledge),
            "input_hash": job.input_hash}, usage=redact_value(usage or {}),
        duration_ms=(usage or {}).get("duration_ms"), created_at=instant))


async def commit_candidate(lease, result, config):
    from memory.wiki.automatic import is_verified
    from memory.wiki.service import publish_candidate_in_session
    if config.automatic_knowledge and not is_verified(result):
        raise WikiStateError("wiki_automatic_grounding_required")
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=lease.user_id)
        job = await db.scalar(select(MemoryWikiJob).where(*_fence(lease)).with_for_update())
        if job is None or not config.enabled("wiki", lease.user_id):
            raise WikiStateError("wiki_lease_lost")
        await _organization_available(db, job)
        scope = await resolve_access_scope(db, user_id=lease.user_id, workspace_id=lease.workspace_id,
                                          project_id=lease.project_id)
        spec = job.spec
        await read_sources(db, scope, spec["sources"], spec["memories"], acl_epoch=spec["acl_epoch"], lock=True)
        target = await _target(db, scope, job.target_identity, lock=True)
        expected = spec["target"]
        if ((target is None) != (expected["revision"] == 0)
                or target and (await target_is_deleted(db, target) or target.revision != expected["revision"] or target.content_hash != expected["content_hash"]
                               or target.body is not None and text_hash(target.body) != target.content_hash)):
            raise WikiStateError("wiki_target_changed")
        draft = result.candidate.serialize()
        if (draft["expected_target"] != expected or draft["domain"] != spec["domain"]
                or draft["model"] != spec["policy"]["model"] or draft["policy_version"] != spec["policy"]["version"]):
            raise WikiStateError("wiki_candidate_contract_changed")
        digest = immutable_candidate_hash(draft, spec["sources"], spec["memories"], spec["acl_epoch"],
                                          expected["revision"], expected["content_hash"])
        candidate = MemoryWikiCandidate(id=ascending("wiki_candidate"), job_id=job.id, user_id=job.user_id,
            workspace_id=job.workspace_id, project_id=job.project_id, target_identity=job.target_identity,
            target_page_id=expected["id"], revision=1, candidate_hash=digest, draft=draft,
            source_manifest=spec["sources"], memory_manifest=spec["memories"], acl_epoch=spec["acl_epoch"],
            expected_target_revision=expected["revision"], expected_target_hash=expected["content_hash"],
            input_hash=job.input_hash, status="PENDING", usage=redact_value(result.usage), created_at=now())
        db.add(candidate)
        job.status, job.candidate_id, job.updated_at = "COMPLETED", candidate.id, now()
        job.usage, job.lease_until = redact_value(result.usage), None
        await _trace(db, job, config, status="completed", reason_code="cache_hit" if result.reused else "wiki_candidate_compiled",
                     usage=result.usage, candidate_id=candidate.id)
        await db.flush()
        if config.automatic_knowledge:
            await publish_candidate_in_session(db, user_id=job.user_id, workspace_id=job.workspace_id,
                candidate_id=candidate.id, candidate_revision=candidate.revision, approved_hash=candidate.candidate_hash,
                expected_target_revision=candidate.expected_target_revision,
                expected_target_hash=candidate.expected_target_hash, request_id=f"automatic:{job.id}",
                config=config, automatic=True)
        return candidate.id


async def fail_job(lease, code, config, *, terminal=False, usage=None):
    async with get_db_session() as db:
        job = await db.scalar(select(MemoryWikiJob).where(*_fence(lease)).with_for_update())
        if job is None:
            return
        status = "CANCELLED" if terminal else "FAILED" if job.attempts >= config.max_attempts else "RETRY"
        job.status, job.last_error, job.updated_at = status, code[:64], now()
        job.available_at, job.lease_until = now() + timedelta(seconds=min(300, 2 ** job.attempts)), None
        job.usage = redact_value(usage or {})
        await _trace(db, job, config, status=status.lower(), reason_code=code, usage=usage)


class MemoryWikiWorker:
    def __init__(self, config=None, *, model=None, verifier=None):
        self.config = config
        self.model = model
        self.verifier = verifier
        self.owner = f"wiki-worker:{uuid.uuid4().hex}"
        self._task = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    def _config(self):
        if self.config is not None:
            return self.config
        from core.config import get_config
        return get_config().memory

    def start(self):
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="memory-wiki")

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
            lease = await claim_job(self.owner, config)
            if lease is None:
                return False
            model = self.model or ConfiguredWikiModel(config)
            try:
                model = await _budgeted_model(lease, model)
                request = await read_request(lease, config)
                if config.automatic_knowledge:
                    from memory.wiki.automatic import compile_automatic
                    compilation = compile_automatic(request, model=model, cache=SQLCompilationCache(lease),
                        config=config, verifier=self.verifier, reserve=getattr(model, "reserve", None),
                        admitted=await admitted_fallback(lease))
                else:
                    compilation = compile_candidate(request, model=model, cache=SQLCompilationCache(lease))
                result = await asyncio.wait_for(compilation, timeout=config.compilation_timeout_seconds * 2 + 5)
                await commit_candidate(lease, result, config)
            except (WikiStateError, MemoryAccessDenied, WikiContractError) as exc:
                code = exc.code if isinstance(exc, WikiStateError) else "wiki_policy_denied" if isinstance(exc, MemoryAccessDenied) else str(exc)
                if code in {"wiki_organization_budget_exhausted", "wiki_maintenance_budget_exhausted"}:
                    await _pause_budget(lease, code)
                else:
                    await fail_job(lease, code, config, terminal=True, usage=getattr(model, "last_usage", None))
            except (MemoryProviderError, asyncio.TimeoutError) as exc:
                code = exc.code if isinstance(exc, MemoryProviderError) else "wiki_provider_timeout"
                await fail_job(lease, code, config, usage=getattr(model, "last_usage", None))
            except asyncio.CancelledError:
                await fail_job(lease, "wiki_worker_stopped", config)
                raise
            return True

    async def _loop(self):
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Wiki worker recovery pending error_type=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._config().worker_interval_seconds)
            except asyncio.TimeoutError:
                pass
