"""Bounded current SQL task facts, with reauthorized historical snapshots.

Execution progress can change between provider steps. A previous snapshot is
evidence of what SQL reported for that request, never today's task state. Its
private scope and immutable result identity must still be available. Every new
ordinary request receives a fresh snapshot; report-only requests receive none.
"""
import json

from sqlalchemy import case, select

from assistant.commands import command_digest, read_task_scopes
from assistant.policy import AssistantError
from assistant.reads import get_task, result_view
from db.models.assistant import AssistantTask, TaskResult
from db.models.project import Project
from db.models.session import Session
from memory.redaction import redact_credentials

MAX_CONTEXT_TASKS = 12
MAX_TASK_SNAPSHOTS = 200


def _scope(task):
    return command_digest({key: getattr(task, key) for key in (
        "id", "assistant_session_id", "user_id", "workspace_id", "project_id", "execution_session_id", "title")})


async def _view(db, main, task_id):
    value = await get_task(db=db, user_id=main.user_id, workspace_id=main.workspace_id,
                           main_id=main.id, task_id=task_id)
    value = json.loads(json.dumps(value, default=str))
    value["task"]["title"] = redact_credentials(value["task"]["title"])
    return value


async def task_context(db, main):
    # Scope is applied before ranking/limiting: neither counts nor labels of
    # unavailable tasks enter the projection. Do not wake their executions.
    tasks = list((await db.scalars(select(AssistantTask).join(
        Session, Session.id == AssistantTask.execution_session_id).join(
        Project, Project.id == AssistantTask.project_id).where(
        AssistantTask.assistant_session_id == main.id, AssistantTask.user_id == main.user_id,
        AssistantTask.workspace_id == main.workspace_id, AssistantTask.archived_at.is_(None),
        Session.project_id == AssistantTask.project_id, Session.user_id == main.user_id,
        Session.workspace_id == main.workspace_id, Session.is_deleted.is_(False),
        Session.kind == "normal", Session.visibility == "private", Session.memory_policy == "assistant_isolated",
        Project.user_id == main.user_id, Project.workspace_id == main.workspace_id, Project.is_deleted.is_(False))
        .order_by(case((AssistantTask.observed_state == "waiting_input", 0),
                       (AssistantTask.observed_state.in_(("queued", "running")), 1), else_=2),
                  AssistantTask.updated_at.desc(), AssistantTask.id.desc())
        .limit(MAX_CONTEXT_TASKS + 1))).all())
    items, refs = [], []
    for task in tasks[:MAX_CONTEXT_TASKS]:
        snapshot = await _view(db, main, task.id)
        items.append(snapshot)
        refs.append({"task_id": task.id, "scope_digest": _scope(task), "snapshot": snapshot,
                     "snapshot_digest": command_digest(snapshot)})
    return {"items": items, "has_more": len(tasks) > MAX_CONTEXT_TASKS,
            "complete_inventory": False, "selection": "waiting_input, active, then recent; archived tasks omitted",
            "read_more": "Use tasks.list and its cursor for the full authorized list, then tasks.get/results.read for details.",
            "untrusted_data": True, "grants_authority": False}, refs


async def validate_task_snapshots(db, main, refs, *, fresh=False, snapshot_checks=None):
    if not isinstance(refs, list) or len(refs) > MAX_TASK_SNAPSHOTS:
        raise AssistantError(410, "ASSISTANT_TASK_SNAPSHOT_UNVERIFIED", "Task snapshots exceed their verification budget")
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("snapshot"), dict):
            raise AssistantError(410, "ASSISTANT_TASK_SNAPSHOT_UNVERIFIED", "Task snapshot is incomplete")
    if snapshot_checks is not None and not fresh:
        for ref in refs:
            await snapshot_checks.check(db, "task_snapshot", (main.user_id, main.workspace_id, main.id), ref,
                lambda: validate_task_snapshots(db, main, [ref]))
        return
    for ref in refs:
        snapshot = ref["snapshot"]
        if command_digest(snapshot) != ref.get("snapshot_digest"):
            raise AssistantError(410, "ASSISTANT_TASK_SNAPSHOT_UNVERIFIED", "Task snapshot changed")
        if (not isinstance(snapshot.get("task"), dict)
                or snapshot.get("latest_result") is not None and not isinstance(snapshot["latest_result"], dict)):
            raise AssistantError(410, "ASSISTANT_TASK_SNAPSHOT_UNVERIFIED", "Task snapshot is incomplete")
    tasks = await read_task_scopes(db, user_id=main.user_id, workspace_id=main.workspace_id,
                                  main_id=main.id, task_ids=[ref.get("task_id") for ref in refs])
    result_ids = [ref["snapshot"]["latest_result"].get("result_id")
        for ref in refs if ref["snapshot"].get("latest_result")]
    if any(not isinstance(result_id, str) or not result_id for result_id in result_ids):
        raise AssistantError(410, "ASSISTANT_TASK_SNAPSHOT_CHANGED", "The original task result changed")
    result_ids = list(dict.fromkeys(result_ids))
    results = {}
    for offset in range(0, len(result_ids), 100):
        rows = await db.scalars(select(TaskResult).where(TaskResult.id.in_(result_ids[offset:offset + 100]))
                                .execution_options(populate_existing=True))
        results.update((row.id, row) for row in rows)
    for ref, (task, _) in zip(refs, tasks):
        snapshot = ref["snapshot"]
        value = snapshot.get("task") or {}
        if (_scope(task) != ref.get("scope_digest") or value.get("id") != task.id
                or value.get("project_id") != task.project_id or value.get("execution_session_id") != task.execution_session_id
                or any(type(value.get(key)) is not int or not 1 <= value[key] <= getattr(task, key)
                       for key in ("control_revision", "intent_revision"))):
            raise AssistantError(410, "ASSISTANT_TASK_SNAPSHOT_CHANGED", "Task scope or source identity changed")
        result = snapshot.get("latest_result")
        if result:
            row = results.get(result["result_id"])
            current = result_view(row)
            if row is None or row.task_id != task.id or any(result.get(key) != current.get(key) for key in (
                    "run_id", "generation", "result_message_id", "outcome", "observed_intent_revision", "created_at")):
                raise AssistantError(410, "ASSISTANT_TASK_SNAPSHOT_CHANGED", "The original task result changed")
        if fresh and command_digest(await _view(db, main, task.id)) != ref["snapshot_digest"]:
            raise AssistantError(409, "ASSISTANT_TASK_SNAPSHOT_CHANGED", "Task progress changed before this request; refresh its snapshot")
