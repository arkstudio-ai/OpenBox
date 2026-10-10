"""Transactional task admission and restart-safe observation (no profile code)."""
from dataclasses import asdict

from sqlalchemy import select

from core import config as runtime_config
from core.identifier import ascending
from db.models.memory_wiki import MemoryWikiJob
from db.models.wiki_platform import WikiOrganizationRun
from memory.wiki import organization, service
from wiki_compiler.hashing import canonical_hash
from wiki_compiler.profiles import require


async def prepare(db, scope, run, stage, payload):
    config = runtime_config.get_config().memory
    organization.require_enabled(config, scope.user_id)
    require(stage.get("action") in {"organize", "compile"}, "action", "wiki_workflow_no_task")
    require(payload.get("confirm_cost") is True, "confirm_cost", "wiki_cost_confirmation_required")
    if stage["action"] == "organize":
        snapshot = await organization.inventory(db, scope, config)
        require(payload.get("input_hash") == snapshot["input_hash"], "input_hash", "wiki_organization_preview_changed")
        budget = payload.get("max_model_calls")
        require(type(budget) is int and 1 <= budget <= config.wiki_organization_max_calls, "budget", "wiki_organization_invalid_budget")
        queued = await organization.enqueue(db, scope, snapshot, request_id="workflow:" + run.id + ":" + stage["id"],
            max_model_calls=budget, compile_pages=True, config=config)
        return {"kind": "organize", "id": queued.id}
    memory_ids = payload.get("memory_ids")
    require(isinstance(memory_ids, list) and 0 < len(memory_ids) <= 12 and all(isinstance(item, str) for item in memory_ids), "memory_ids")
    frozen = await service.freeze_compile(db, scope, slug=payload.get("slug", ""), title=payload.get("title", ""),
        memory_ids=memory_ids, model=config.extract_model or runtime_config.get_config().model)
    request = frozen["request"]
    queued = MemoryWikiJob(id=ascending("wiki_job"), user_id=scope.user_id, workspace_id=scope.workspace_id,
        project_id=scope.project_id, target_identity=frozen["target_identity"], input_hash=frozen["input_hash"],
        request_id="workflow:" + canonical_hash({"run": run.id, "stage": stage["id"]}),
        spec={"schema_version": "wiki-job-v1", "slug": request.slug, "title": request.title,
              "domain": request.domain, "target": asdict(request.target), "policy": asdict(request.policy),
              "sources": frozen["sources"], "memories": frozen["memories"], "acl_epoch": scope.acl_epoch},
        status="PENDING", attempts=0, lease_generation=0, available_at=service.now(),
        created_at=service.now(), updated_at=service.now())
    db.add(queued)
    await db.flush()
    return {"kind": "compile", "id": queued.id}


async def task_view(db, scope, task):
    if not task:
        return None
    model = WikiOrganizationRun if task["kind"] == "organize" else MemoryWikiJob
    row = await db.scalar(select(model).where(model.id == task["id"], model.user_id == scope.user_id,
        model.workspace_id == scope.workspace_id, model.project_id == scope.project_id))
    if row is None:
        return {**task, "status": "unavailable"}
    return {**task, **(organization.run_view(row) if task["kind"] == "organize" else service._job_view(row))}


async def task_completed(db, scope, task):
    view = await task_view(db, scope, task)
    require(view is not None and view["status"] == "completed", "task", "wiki_workflow_task_incomplete")
    if task["kind"] == "organize":
        run = await db.get(WikiOrganizationRun, task["id"])
        current = await organization.inventory(db, scope, runtime_config.get_config().memory)
        require(current["input_hash"] == run.input_hash, "task", "wiki_organization_inputs_changed")
    else:
        job = await db.get(MemoryWikiJob, task["id"])
        await service.read_sources(db, scope, job.spec["sources"], job.spec["memories"], acl_epoch=job.spec["acl_epoch"])


async def retry(db, scope, task, payload):
    require(payload.get("confirm_cost") is True, "confirm_cost", "wiki_cost_confirmation_required")
    config = runtime_config.get_config().memory
    organization.require_enabled(config, scope.user_id)
    view = await task_view(db, scope, task)
    require(view is not None and view["status"] in {"failed", "paused", "partial"}, "task", "wiki_workflow_task_not_retryable")
    row = await db.get(WikiOrganizationRun if task["kind"] == "organize" else MemoryWikiJob, task["id"])
    if task["kind"] == "organize":
        require(organization.run_view(row)["can_resume"], "task", "wiki_organization_selection_required")
        snapshot = await organization.inventory(db, scope, config)
        require(row.input_hash == snapshot["input_hash"], "task", "wiki_organization_inputs_changed")
        budget = payload.get("max_model_calls")
        require(type(budget) is int and row.model_calls < budget <= config.wiki_organization_max_calls, "budget", "wiki_organization_invalid_budget")
        row.spec = {**row.spec, "max_model_calls": budget}
        row.result = {**row.result, "phase": "compiling"} if row.result.get("phase") == "complete" else row.result
        row.reason_code, row.revision = None, row.revision + 1
        for page in row.result.get("pages", []):
            child = await db.get(MemoryWikiJob, page["job_id"]) if page.get("job_id") else None
            if child and child.spec.get("organization_id") == row.id and child.status in {"PAUSED", "FAILED"}:
                child.status, child.attempts, child.available_at = "PENDING", 0, service.now()
    else:
        await service.read_sources(db, scope, row.spec["sources"], row.spec["memories"], acl_epoch=row.spec["acl_epoch"])
        row.last_error = None
    row.status, row.attempts, row.available_at = "PENDING", 0, service.now()
    row.lease_until, row.lease_generation, row.updated_at = None, row.lease_generation + 1, service.now()
    return task
