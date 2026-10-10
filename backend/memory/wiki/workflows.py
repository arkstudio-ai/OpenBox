"""Durable versioned workflows. Output application and advancement are atomic."""
from copy import deepcopy
from types import SimpleNamespace

from sqlalchemy import select

from core.identifier import ascending
from db.base import get_db_session
from db.models.wiki_workflow import WikiWorkflowEvent, WikiWorkflowRun
from memory.policy import resolve_access_scope
from memory.service import lock_memory_authority
from memory.wiki import profiles, workflow_tasks
from memory.wiki.organization import scoped_values
from memory.wiki.service import WikiStateError, now
from memory.wiki.workflow_outputs import apply_output, output_visible, validate_outputs
from wiki_compiler.hashing import canonical_hash
from wiki_compiler.profiles import require, validate_fields


def stages_for(run):
    return run.definition["workflows"][run.workflow_id]["stages"]


def stage_state():
    return {"status": "pending", "outputs": [], "output_hash": canonical_hash([]), "approvals": {}, "results": [], "task": None}


def frozen_profile(run):
    return SimpleNamespace(id=run.profile_id, definition=run.definition)


def approval_digest(run, stage, state):
    return canonical_hash({"run": run.id, "stage": stage["id"], "profile": run.profile_hash,
                           "outputs": state["outputs"], "task": state.get("task")})


async def run_view(db, scope, run, profile):
    states = {}
    for key, state in run.stages.items():
        visible = all([await output_visible(db, scope, frozen_profile(run), output) for output in state.get("outputs", [])])
        states[key] = {**state, "outputs": state["outputs"] if visible else [],
            "outputs_available": visible, "results": state.get("results", []) if visible else [],
            "task": await workflow_tasks.task_view(db, scope, state.get("task"))}
    definition = stages_for(run)
    return {"id": run.id, "revision": run.revision, "project_id": run.project_id, "profile_id": run.profile_id,
            "profile_hash": run.profile_hash, "workflow_id": run.workflow_id, "inputs": run.inputs,
            "status": run.status.lower(), "reason": run.reason, "stage_index": run.stage_index,
            "current_stage": definition[run.stage_index]["id"] if run.stage_index < len(definition) else None,
            "definition": run.definition, "stages": states,
            "definition_changed": profile is None or profile.definition_hash != run.profile_hash,
            "created_at": run.created_at.isoformat(), "updated_at": run.updated_at.isoformat()}


def event(db, run, *, request_id, request_hash, action, stage_id=None, detail=None):
    db.add(WikiWorkflowEvent(id=ascending("wiki_event"), run_id=run.id, request_id=request_id,
        request_hash=request_hash, version=run.revision, action=action, stage_id=stage_id,
        detail=detail or {}, actor_id=run.user_id, created_at=now().isoformat()))


async def start(*, user_id, workspace_id, profile_id, expected_profile_revision, workflow_id, inputs, request_id):
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        profile = await profiles.get_profile(db, scope, profile_id)
        if profile is None:
            return None
        local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=profile.project_id)
        workflow = profile.definition["workflows"].get(workflow_id)
        require(workflow is not None, "workflow", "wiki_workflow_unknown")
        inputs = validate_fields(inputs, workflow.get("inputs", {}), "inputs")
        previous = await db.scalar(select(WikiWorkflowRun).where(WikiWorkflowRun.user_id == user_id,
            WikiWorkflowRun.workspace_id == scope.workspace_id, WikiWorkflowRun.request_id == request_id))
        if previous:
            require(previous.profile_id == profile_id and previous.workflow_id == workflow_id and previous.inputs == inputs,
                    "request_id", "wiki_request_conflict")
            return await run_view(db, local, previous, profile)
        require(profile.revision == expected_profile_revision, "profile", "wiki_profile_changed")
        run = WikiWorkflowRun(id=ascending("wiki_flow"), **scoped_values(local), profile_id=profile.id,
            profile_hash=profile.definition_hash, definition=profile.definition, workflow_id=workflow_id,
            request_id=request_id, inputs=inputs, status="RUNNING", stage_index=0,
            stages={stage["id"]: stage_state() for stage in workflow["stages"]})
        run.stages[workflow["stages"][0]["id"]]["status"] = "running"
        db.add(run)
        event(db, run, request_id=request_id, request_hash=canonical_hash(inputs), action="start")
        await db.flush()
        return await run_view(db, local, run, profile)


async def _find(db, user_id, workspace_id, run_id):
    scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
    run = await db.scalar(select(WikiWorkflowRun).where(WikiWorkflowRun.id == run_id,
        *scope.predicates(WikiWorkflowRun)).with_for_update())
    if run is None:
        return None, None, None
    local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=run.project_id)
    return run, local, await profiles.get_profile(db, local, run.profile_id)


async def detail(*, user_id, workspace_id, run_id, event_offset=0):
    async with get_db_session() as db:
        run, scope, profile = await _find(db, user_id, workspace_id, run_id)
        if run is None:
            return None
        result = await run_view(db, scope, run, profile)
        rows = list((await db.scalars(select(WikiWorkflowEvent).where(WikiWorkflowEvent.run_id == run.id)
            .order_by(WikiWorkflowEvent.version).offset(event_offset).limit(101))).all())
        result["events"] = [{"id": row.id, "version": row.version, "action": row.action,
            "stage_id": row.stage_id, "detail": row.detail, "created_at": row.created_at} for row in rows[:100]]
        result["events_next_offset"] = event_offset + 100 if len(rows) > 100 else None
        return result


async def list_runs(*, user_id, workspace_id, project_id=None, offset=0):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=project_id is None)
        rows = list((await db.scalars(select(WikiWorkflowRun).where(*scope.predicates(WikiWorkflowRun))
            .order_by(WikiWorkflowRun.updated_at.desc(), WikiWorkflowRun.id).offset(offset).limit(41))).all())
        return {"runs": [{"id": run.id, "revision": run.revision, "workflow_id": run.workflow_id,
            "profile_id": run.profile_id, "status": run.status.lower(), "stage_index": run.stage_index,
            "updated_at": run.updated_at.isoformat()} for run in rows[:40]],
            "next_offset": offset + 40 if len(rows) > 40 else None}


async def _validate_stage(db, scope, run, stage, state):
    require(len(state["outputs"]) >= stage.get("outputsRequired", 0), "outputs", "wiki_workflow_outputs_required")
    await preflight_outputs(db, scope, run, stage, state["outputs"])
    if stage.get("action"):
        await workflow_tasks.task_completed(db, scope, state.get("task"))


async def _submit(db, scope, run, stage, state, payload):
    outputs = validate_outputs(payload.get("outputs"))
    await preflight_outputs(db, scope, run, stage, outputs)
    state.update({"outputs": outputs, "output_hash": canonical_hash(outputs), "approvals": {}, "status": "awaiting_review"})
    return {"output_hash": state["output_hash"], "count": len(outputs)}


async def preflight_outputs(db, scope, run, stage, outputs):
    """Validate the ordered, projected state without publishing any writes.

    A later output may refer to a revision produced by an earlier output. Using
    the same SQL write path also detects conflicts within the batch before a
    reviewer approves it. These handlers only mutate SQL in this transaction.
    """
    savepoint = await db.begin_nested()
    try:
        for output in outputs:
            await apply_output(db, scope, frozen_profile(run), stage, output, apply=True, run_id=run.id)
        await db.flush()
    finally:
        await savepoint.rollback()


async def _approve(db, scope, run, stage, state, payload):
    gate = payload.get("gate")
    require(gate in stage["gates"] and gate.startswith("human:"), "gate", "wiki_workflow_gate_denied")
    require(payload.get("output_hash") == state["output_hash"], "output_hash", "wiki_workflow_output_changed")
    await _validate_stage(db, scope, run, stage, state)
    state["approvals"][gate] = {"digest": approval_digest(run, stage, state), "actor_id": scope.user_id, "at": now().isoformat()}
    return {"gate": gate, "digest": state["approvals"][gate]["digest"]}


async def _advance(db, scope, run, stage, state):
    await _validate_stage(db, scope, run, stage, state)
    for gate in stage["gates"]:
        if gate.startswith("human:"):
            require(state["approvals"].get(gate, {}).get("digest") == approval_digest(run, stage, state), "gate", "wiki_workflow_gate_required")
        elif gate == "agent:task-completed":
            await workflow_tasks.task_completed(db, scope, state.get("task"))
        # trust:sources-current is enforced for every output by _validate_stage.
    results = []
    for output in state["outputs"]:
        results.append(await apply_output(db, scope, frozen_profile(run), stage, output, apply=True, run_id=run.id))
    state["status"], state["results"] = "completed", results
    run.stage_index += 1
    if run.stage_index == len(stages_for(run)):
        run.status = "COMPLETED"
    return {"results": [{key: value for key, value in result.items() if key in {"id", "revision", "content_hash"}} for result in results]}


async def _cancel_tasks(db, run):
    from db.models.memory_wiki import MemoryWikiJob
    from db.models.wiki_platform import WikiOrganizationRun
    from memory.wiki.organization import cancel_page_jobs
    for state in run.stages.values():
        task = state.get("task")
        if not task:
            continue
        row = await db.get(WikiOrganizationRun if task["kind"] == "organize" else MemoryWikiJob, task["id"])
        if row and row.status in {"PENDING", "RUNNING", "RETRY", "PAUSED"}:
            row.status, row.lease_until, row.lease_generation = "CANCELLED", None, row.lease_generation + 1
            if task["kind"] == "organize":
                row.revision, row.reason_code = row.revision + 1, "wiki_workflow_cancelled"
                await cancel_page_jobs(db, row)
            else:
                row.last_error = "wiki_workflow_cancelled"


async def act(*, user_id, workspace_id, run_id, expected_revision, request_id, action, payload):
    require(len(str(payload)) <= 300000, "payload", "wiki_workflow_output_limit")
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        run, scope, profile = await _find(db, user_id, workspace_id, run_id)
        if run is None:
            return None
        digest = canonical_hash({"action": action, "payload": payload, "expected_revision": expected_revision})
        previous = await db.scalar(select(WikiWorkflowEvent).where(WikiWorkflowEvent.run_id == run.id,
            WikiWorkflowEvent.request_id == request_id))
        if previous:
            require(previous.request_hash == digest, "request_id", "wiki_request_conflict")
            return await run_view(db, scope, run, profile)
        require(run.revision == expected_revision, "revision", "wiki_workflow_changed")
        require(run.status not in {"COMPLETED", "CANCELLED"}, "status", "wiki_workflow_terminal")
        require(profile is not None, "profile", "wiki_profile_unavailable")
        if action not in {"cancel", "adapt"}:
            require(profile.definition_hash == run.profile_hash, "definition", "wiki_workflow_definition_changed")
        stage = stages_for(run)[run.stage_index]
        states = deepcopy(run.stages)
        state = states[stage["id"]]
        result = {}
        if action == "cancel":
            await _cancel_tasks(db, run)
            run.status, state["status"] = "CANCELLED", "cancelled"
        elif action == "fail":
            require(run.status == "RUNNING", "status", "wiki_workflow_action_denied")
            require(isinstance(payload.get("reason"), str) and 1 <= len(payload["reason"]) <= 800, "reason")
            run.status, run.reason, state["status"] = "FAILED", payload["reason"], "failed"
        elif action == "resume":
            require(run.status == "FAILED", "status", "wiki_workflow_action_denied")
            run.status, run.reason, state["status"], state["approvals"] = "RUNNING", None, "running", {}
        elif action == "adapt":
            from memory.wiki.workflow_adaptation import apply_adaptation
            result = await apply_adaptation(db, scope, run, profile, payload)
            states = deepcopy(run.stages)
        else:
            require(run.status == "RUNNING", "status", "wiki_workflow_action_denied")
            if action == "submit":
                result = await _submit(db, scope, run, stage, state, payload)
            elif action == "approve":
                result = await _approve(db, scope, run, stage, state, payload)
            elif action == "advance":
                result = await _advance(db, scope, run, stage, state)
                if run.status != "COMPLETED":
                    states[stages_for(run)[run.stage_index]["id"]]["status"] = "running"
            elif action == "prepare":
                require(not state.get("task"), "task", "wiki_workflow_task_already_started")
                state["task"] = await workflow_tasks.prepare(db, scope, run, stage, payload)
                state["approvals"] = {}
                result = state["task"]
            elif action == "retry_task":
                result = await workflow_tasks.retry(db, scope, state.get("task"), payload)
                state["approvals"] = {}
            else:
                raise WikiStateError("wiki_workflow_action_denied")
        run.stages, run.updated_at, run.revision = states, now(), run.revision + 1
        event(db, run, request_id=request_id, request_hash=digest, action=action, stage_id=stage["id"], detail=result)
        await db.flush()
        return await run_view(db, scope, run, profile)
