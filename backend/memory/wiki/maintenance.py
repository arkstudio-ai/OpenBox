"""Budgeted ongoing Wiki maintenance; consumer enrollment never needs a UI step."""
from datetime import timedelta

from sqlalchemy import and_, or_, select

from core import config as runtime_config
from core.identifier import ascending
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_wiki import MemoryWikiJob
from db.models.wiki_platform import WikiMaintenancePolicy, WikiOrganizationRun
from memory.policy import MemoryAccessDenied, active_memory_predicates, resolve_access_scope
from memory.service import lock_memory_authority
from memory.wiki.organization import enqueue, inventory, require_enabled, scoped_values
from memory.wiki.service import WikiStateError, domain_for, now


def _view(policy):
    if policy is None:
        return {"revision": 0, "enabled": False, "compile_pages": True, "call_limit": 20,
                "calls_used": 0, "window_started_at": None, "last_run_id": None, "reason_code": None}
    return {"id": policy.id, "revision": policy.revision, "enabled": policy.enabled,
            "compile_pages": policy.compile_pages, "call_limit": policy.call_limit, "calls_used": policy.calls_used,
            "window_started_at": policy.window_started_at.isoformat(), "last_run_id": policy.last_run_id,
            "reason_code": policy.reason_code, "next_check_at": policy.next_check_at.isoformat()}


async def read(*, user_id, workspace_id, project_id=None):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        return _view(await db.scalar(select(WikiMaintenancePolicy).where(
            WikiMaintenancePolicy.domain == domain_for(scope, project_id))))


async def configure(*, user_id, workspace_id, project_id, expected_revision, enabled, call_limit, compile_pages):
    config = runtime_config.get_config().memory
    if enabled:
        require_enabled(config, user_id)
    if not 1 <= call_limit <= config.wiki_organization_max_calls:
        raise WikiStateError("wiki_organization_invalid_budget")
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        domain = domain_for(scope, project_id)
        policy = await db.scalar(select(WikiMaintenancePolicy).where(WikiMaintenancePolicy.domain == domain).with_for_update())
        if (policy.revision if policy else 0) != expected_revision:
            raise WikiStateError("wiki_maintenance_changed")
        if policy is None:
            policy = WikiMaintenancePolicy(id=ascending("wiki_maintenance"), **scoped_values(scope), domain=domain,
                calls_used=0, window_started_at=now(), next_check_at=now())
            db.add(policy)
        else:
            policy.revision += 1
        policy.enabled, policy.call_limit, policy.compile_pages = enabled, call_limit, compile_pages
        policy.reason_code, policy.updated_at, policy.next_check_at = None, now(), now()
        # Explicit reconfiguration may enable compiling previously organized
        # concepts, even when their underlying memory snapshot is unchanged.
        policy.last_input_hash = None
        if not enabled:
            from memory.wiki.organization import cancel_page_jobs
            active = list((await db.scalars(select(WikiOrganizationRun).where(
                WikiOrganizationRun.automation_id == policy.id,
                WikiOrganizationRun.status.in_(["PENDING", "RUNNING", "RETRY", "PAUSED"])))) .all())
            for run in active:
                run.status, run.reason_code = "CANCELLED", "wiki_maintenance_disabled"
                run.lease_until, run.lease_generation = None, run.lease_generation + 1
                run.revision, run.updated_at = run.revision + 1, now()
                await cancel_page_jobs(db, run)
        await db.flush()
        return _view(policy)


async def request_recheck(db, scope):
    policy = await db.scalar(select(WikiMaintenancePolicy).where(
        WikiMaintenancePolicy.domain == domain_for(scope, scope.project_id)))
    if policy:
        policy.last_input_hash, policy.next_check_at = None, now()


async def ensure_automatic(db, scope, config):
    if not config.automatic_knowledge or not config.enabled("wiki", scope.user_id):
        return None
    domain = domain_for(scope, scope.project_id)
    policy = await db.scalar(select(WikiMaintenancePolicy).where(WikiMaintenancePolicy.domain == domain))
    if policy is None:
        policy = WikiMaintenancePolicy(id=ascending("wiki_maintenance"), **scoped_values(scope), domain=domain,
            enabled=True, automatic=True, compile_pages=True,
            call_limit=min(config.wiki_auto_daily_calls, config.wiki_organization_max_calls),
            calls_used=0, window_started_at=now(), next_check_at=now())
        db.add(policy)
    elif not policy.automatic:
        # Explicit consumer-mode rollout migrates the old manual enrollment once.
        policy.automatic, policy.enabled, policy.compile_pages = True, True, True
        policy.call_limit = min(config.wiki_auto_daily_calls, config.wiki_organization_max_calls)
        policy.revision, policy.last_input_hash, policy.next_check_at = policy.revision + 1, None, now()
    await db.flush()
    return policy


async def discover_automatic(config):
    if not config.automatic_knowledge:
        return
    async with get_db_session() as db:
        stmt = select(UserMemory.user_id, UserMemory.workspace_id, UserMemory.project_id).outerjoin(
            WikiMaintenancePolicy, and_(WikiMaintenancePolicy.user_id == UserMemory.user_id,
                WikiMaintenancePolicy.workspace_id == UserMemory.workspace_id,
                WikiMaintenancePolicy.project_id.is_not_distinct_from(UserMemory.project_id))).where(
            *active_memory_predicates(), or_(WikiMaintenancePolicy.id.is_(None), WikiMaintenancePolicy.automatic.is_(False)))
        if config.allowed_user_ids:
            stmt = stmt.where(UserMemory.user_id.in_(config.allowed_user_ids))
        scopes = (await db.execute(stmt.distinct().limit(20))).all()
    for user_id, workspace_id, project_id in scopes:
        async with get_db_session() as db:
            try:
                await lock_memory_authority(db, user_id=user_id)
                scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
                await ensure_automatic(db, scope, config)
            except MemoryAccessDenied:
                continue


async def reserve_policy_call(policy_id, config):
    async with get_db_session() as db:
        identity = await db.get(WikiMaintenancePolicy, policy_id)
        if identity is None:
            raise WikiStateError("wiki_maintenance_disabled")
        await lock_memory_authority(db, user_id=identity.user_id)
        policy = await db.scalar(select(WikiMaintenancePolicy).where(WikiMaintenancePolicy.id == policy_id).with_for_update())
        if not policy.enabled or not config.automatic_knowledge or not config.enabled("wiki", policy.user_id):
            raise WikiStateError("wiki_maintenance_disabled")
        if policy.window_started_at.replace(tzinfo=now().tzinfo) + timedelta(hours=24) <= now():
            policy.window_started_at, policy.calls_used = now(), 0
        if policy.calls_used >= policy.call_limit:
            raise WikiStateError("wiki_maintenance_budget_exhausted")
        policy.calls_used += 1


async def _resume_budget(db, policy, run, config):
    remaining = policy.call_limit - policy.calls_used
    maximum = min(config.wiki_organization_max_calls, run.model_calls + remaining)
    if maximum <= run.model_calls:
        policy.reason_code = "wiki_maintenance_budget_exhausted"
        return
    run.spec = {**run.spec, "max_model_calls": maximum}
    run.status, run.attempts, run.reason_code, run.available_at = "PENDING", 0, None, now()
    run.lease_until, run.lease_generation, run.revision = None, run.lease_generation + 1, run.revision + 1
    for page in run.result.get("pages", []):
        job = await db.get(MemoryWikiJob, page.get("job_id")) if page.get("job_id") else None
        if job and job.spec.get("organization_id") == run.id and job.status == "PAUSED":
            job.status, job.available_at, job.attempts = "PENDING", now(), 0
    policy.reason_code = None


async def _tick(policy_id, config):
    async with get_db_session() as db:
        identity = await db.get(WikiMaintenancePolicy, policy_id)
        if identity is None:
            return
        await lock_memory_authority(db, user_id=identity.user_id)
        policy = await db.scalar(select(WikiMaintenancePolicy).where(
            WikiMaintenancePolicy.id == policy_id).with_for_update())
        if not policy.enabled or not config.enabled("wiki", policy.user_id) or policy.automatic and not config.automatic_knowledge:
            return
        policy.next_check_at = now() + timedelta(seconds=config.wiki_auto_scan_seconds if policy.automatic else max(5, config.worker_interval_seconds))
        if policy.window_started_at.replace(tzinfo=now().tzinfo) + timedelta(hours=24) <= now():
            policy.window_started_at, policy.calls_used = now(), 0
        try:
            scope = await resolve_access_scope(db, user_id=policy.user_id, workspace_id=policy.workspace_id,
                                              project_id=policy.project_id)
            snapshot = await inventory(db, scope, config)
        except (WikiStateError, MemoryAccessDenied) as exc:
            policy.reason_code = exc.code if isinstance(exc, WikiStateError) else "wiki_policy_denied"
            return
        active = await db.scalar(select(WikiOrganizationRun).where(WikiOrganizationRun.domain == policy.domain,
            WikiOrganizationRun.status.in_(["PENDING", "RUNNING", "RETRY", "PAUSED"]))
            .order_by(WikiOrganizationRun.created_at).limit(1))
        if active:
            if active.automation_id != policy.id:
                policy.reason_code = "wiki_manual_organization_in_progress"
                return
            if active.input_hash != snapshot["input_hash"]:
                active.status, active.reason_code, active.lease_until = "CANCELLED", "wiki_organization_inputs_changed", None
                active.lease_generation, active.revision = active.lease_generation + 1, active.revision + 1
                for item in active.result.get("pages", []):
                    job = await db.get(MemoryWikiJob, item.get("job_id")) if item.get("job_id") else None
                    if job and job.spec.get("organization_id") == active.id and job.status in {"PENDING", "RETRY", "PAUSED"}:
                        job.status, job.last_error = "CANCELLED", "wiki_organization_inputs_changed"
            elif active.status == "PAUSED" and active.reason_code in {
                    "wiki_organization_budget_exhausted", "wiki_maintenance_budget_exhausted"}:
                await _resume_budget(db, policy, active, config)
                return
            else:
                return
        if policy.last_input_hash == snapshot["input_hash"]:
            if policy.automatic and policy.last_run_id and policy.calls_used < policy.call_limit:
                previous = await db.get(WikiOrganizationRun, policy.last_run_id)
                if (previous and previous.status == "FAILED" and previous.input_hash == snapshot["input_hash"]
                        and previous.updated_at.replace(tzinfo=now().tzinfo) + timedelta(minutes=5) <= now()):
                    # Provider/schema failures retry unattended, with a cooldown
                    # and the same shared daily call ceiling. No UI resume gate.
                    previous.status, previous.attempts, previous.reason_code = "PENDING", 0, None
                    previous.available_at, previous.updated_at = now(), now()
                    previous.spec = {**previous.spec, "max_model_calls": min(config.wiki_organization_max_calls,
                        previous.model_calls + policy.call_limit - policy.calls_used)}
            return
        remaining = policy.call_limit - policy.calls_used
        if remaining <= 0:
            policy.reason_code = "wiki_maintenance_budget_exhausted"
            return
        run = await enqueue(db, scope, snapshot, request_id=ascending("wiki_auto_request"),
            max_model_calls=min(remaining, config.wiki_organization_max_calls), compile_pages=policy.compile_pages,
            config=config, automation_id=policy.id)
        policy.last_input_hash, policy.last_run_id, policy.reason_code = snapshot["input_hash"], run.id, None
        policy.updated_at = now()


async def schedule_due(config):
    if not config.wiki:
        return
    await discover_automatic(config)
    async with get_db_session() as db:
        stmt = select(WikiMaintenancePolicy.id).where(WikiMaintenancePolicy.enabled.is_(True),
                                                    WikiMaintenancePolicy.next_check_at <= now())
        if config.allowed_user_ids:
            stmt = stmt.where(WikiMaintenancePolicy.user_id.in_(config.allowed_user_ids))
        ids = list((await db.scalars(stmt.order_by(WikiMaintenancePolicy.next_check_at).limit(20))).all())
    for policy_id in ids:
        await _tick(policy_id, config)
        if config.automatic_knowledge:
            from memory.wiki.consumer_refresh import refresh_existing
            await refresh_existing(policy_id, config)
