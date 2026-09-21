"""Authorized team business commands, committed through the team journal.

These functions run inside one Writer transaction. They never call models,
providers, filesystem or network services; their Inbox writes share the commit.
"""
from __future__ import annotations

from datetime import timedelta
import json

from agent.subagent_composition import SubagentCompositionError, validate_structured_result
from core.identifier import ascending
from team.errors import TeamError
from team.journal import Writer, utcnow
from team.state import AttemptSnapshot, MemberStatus, TaskSnapshot, check_revision, ready_tasks


def require_coordinator(writer: Writer) -> None:
    actor = writer.actor
    if actor.kind == "server":
        return
    if actor.kind != "member" or actor.member_id != writer.run.root_session_id:
        raise TeamError("AUTHORITY_REVOKED", "Only the coordinator can perform this command.", status=403)


def require_open(writer: Writer) -> None:
    state = writer.state["run"]["state"]
    if state not in {"running", "waiting"}:
        raise TeamError("TEAM_PAUSED" if state in {"pausing", "paused"} else "TEAM_CLOSED", f"Team is {state}.")


def member_status(writer: Writer, member_id: str, **changes) -> dict:
    member = writer.state["members"].get(member_id)
    if member is None:
        raise TeamError("TEAM_MEMBER_NOT_FOUND", "Member does not belong to this team.", current=list(writer.state["members"].values()))
    fields = {key: member[key] for key in MemberStatus.model_fields if key in member}
    return writer.append("team.member", "member", {**fields, **changes})


def run_status(writer: Writer, state: str, **changes) -> dict:
    old = writer.state["run"]
    return writer.append("team.run", "run", {**old, "state": state, "revision": old["revision"] + 1, **changes})


def task_status(writer: Writer, task: dict, **changes) -> dict:
    return writer.append("team.task", "task", {**task, "revision": task["revision"] + 1,
        "updated_at": utcnow().isoformat(), **changes})


async def create_task(writer: Writer, fields: dict) -> dict:
    require_coordinator(writer)
    require_open(writer)
    forbidden = set(fields) - (set(TaskSnapshot.model_fields) - {"id", "state", "revision", "current_attempt", "blocked_reason", "cancellation_reason", "created_at", "updated_at"})
    if forbidden:
        raise TeamError("INVALID_TASK", "Task creation contains server-owned fields.", current=sorted(forbidden), status=422)
    now = utcnow().isoformat()
    task = TaskSnapshot(id=ascending("ttask"), created_at=now, updated_at=now, **fields)
    if task.deliverable:
        task.acceptance_mode = "coordinator"
    member = writer.state["members"].get(task.owner_member_id)
    if member is None or member["membership_state"] != "active":
        raise TeamError("TEAM_MEMBER_NOT_FOUND", "Task owner must be an active member.")
    task.exclusive_group = member.get("exclusive_group") or task.exclusive_group
    if task.output_schema is not None:
        from agent_catalog.schemas import AgentSpec
        task.output_schema = AgentSpec.validate_schema(task.output_schema)
    writer.append("team.task", "task", task.model_dump(mode="json"))
    return {"task": writer.state["tasks"][task.id]}


async def queue_message(writer: Writer, *, to_member_id: str, body: str, kind: str = "message",
                        from_member_id: str | None = None, **references) -> dict:
    require_open(writer)
    sender = writer.actor.member_id
    if writer.actor.kind == "server":
        sender = from_member_id or writer.run.root_session_id
    elif writer.actor.kind != "member" or (from_member_id and from_member_id != sender):
        raise TeamError("AUTHORITY_REVOKED", "Message sender must be the authenticated member.", status=403)
    allowed = {"task_id", "task_attempt_id", "reply_to_message_id", "file_refs"}
    if set(references) - allowed:
        raise TeamError("INVALID_MESSAGE", "Unknown message reference fields.", status=422)
    message = {"id": ascending("tmsg"), "from_member_id": sender, "to_member_id": to_member_id,
        "kind": kind, "body": body, "created_at": utcnow().isoformat(), **references}
    writer.append("team.message.queued", "message", message)
    if kind == "progress":
        writer.append("team.message.recorded", "message", {"id": message["id"], "to_member_id": to_member_id})
    return {"message": writer.state["messages"][message["id"]]}


async def deliver_message(writer: Writer, message: dict) -> str:
    from team.runtime import runtime
    source = {"kind": "team_message", "from_member_id": message["from_member_id"], "message_id": message["id"],
        "task_id": message.get("task_id"), "attempt_id": message.get("task_attempt_id")}
    # Peer mail may be a member's first input, before any task dispatch.
    # Dynamic identity belongs in that input, never in its frozen system text.
    prompt = json.dumps({**member_context(writer, message["to_member_id"]), "message": message["body"]}, ensure_ascii=False)
    inbox_id = await runtime.enqueue(writer.db, writer.run, message["to_member_id"], source,
        f"team:msg:{message['id']}", prompt)
    writer.append("team.message.delivered", "message", {"id": message["id"], "to_member_id": message["to_member_id"], "inbox_id": inbox_id})
    return inbox_id


def member_context(writer: Writer, member_id: str) -> dict:
    member = writer.state["members"][member_id]
    return {"identity": {"team_run_id": writer.run.id, "member_id": member_id,
        "alias": member["alias"], "responsibility": member.get("responsibility", ""),
        "coordinator_id": writer.run.root_session_id},
        "output_dir": f".openbox/teams/{writer.run.id}/{member_id}"}


async def dispatch_ready(writer: Writer) -> dict:
    from team.runtime import runtime
    require_coordinator(writer)
    require_open(writer)
    from team.capacity import deferred
    if deferred(writer.state):
        return {"dispatched": [], "waiting_for": "capacity"}
    active = [a for a in writer.state["attempts"].values() if a["state"] == "running"]
    executing = [a for a in active if writer.state["members"][a["member_id"]]["execution_state"] != "waiting"]
    from team.scheduler import _live
    drivers = await runtime.observe(writer.db, writer.state["members"], writer.run.owner_user_id)
    executing_ids = {a["member_id"] for a in executing} | {mid for mid, driver in drivers.items() if _live(driver)}
    limit = writer.state["policy"]["max_concurrent_members"]
    occupied = {a["member_id"] for a in active}
    from team.resources import occupies_desktop, other_desktop_busy
    desktop_busy = occupies_desktop(writer.state)
    if any(task["exclusive_group"] == "desktop" for task in ready_tasks(writer.state)):
        # All dispatchers lock their own run then the workspace; reads of
        # other runs never lock them, avoiding an inverse run/workspace order.
        desktop_busy |= await other_desktop_busy(writer.db, writer.run, lock=True)
    dispatched = []
    for task in ready_tasks(writer.state):
        if len(executing_ids) + len(dispatched) >= limit:
            break
        member = writer.state["members"][task["owner_member_id"]]
        if member["membership_state"] != "active" or member["id"] in occupied:
            continue
        driver = (await runtime.observe(writer.db, [member["id"]], writer.run.owner_user_id)).get(member["id"])
        if _live(driver):
            continue
        if task["exclusive_group"] == "desktop" and desktop_busy:
            continue
        number = max((a["number"] for a in writer.state["attempts"].values() if a["task_id"] == task["id"]), default=0) + 1
        attempt = AttemptSnapshot(id=ascending("tatt"), task_id=task["id"], member_id=member["id"], number=number, started_at=utcnow().isoformat())
        writer.append("team.attempt", "attempt", attempt.model_dump(mode="json"))
        updated = task_status(writer, task, state="running", current_attempt=attempt.id, blocked_reason=None)
        member_status(writer, member["id"], current_attempt=attempt.id, execution_state="queued", wait_after_seq=None, wait_deadline=None, nudged=False)
        prompt = json.dumps({**member_context(writer, member["id"]), "task": updated, "attempt_id": attempt.id,
            "dependencies": [writer.state["tasks"][dep] for dep in task["dependencies"]]}, ensure_ascii=False)
        await runtime.enqueue(writer.db, writer.run, member["id"],
            {"kind": "team_task", "from_member_id": writer.run.root_session_id, "task_id": task["id"], "attempt_id": attempt.id},
            f"team:task:{attempt.id}", prompt)
        occupied.add(member["id"])
        desktop_busy |= task["exclusive_group"] == "desktop"
        dispatched.append({"member_id": member["id"], "task_id": task["id"], "attempt_id": attempt.id})
    return {"dispatched": dispatched}


async def update_task(writer: Writer, task_id: str, expected_revision: int | None, action: str, *,
                      summary: str = "", output=None, artifacts: list[dict] | None = None,
                      changes: dict | None = None, reason: str | None = None, implicit: bool = False) -> dict:
    require_open(writer)
    task = writer.state["tasks"].get(task_id)
    if task is None:
        raise TeamError("TEAM_TASK_NOT_FOUND", "Task does not belong to this run.", status=404)
    is_coordinator = writer.actor.kind == "server" or writer.actor.member_id == writer.run.root_session_id
    if is_coordinator:
        check_revision(task, expected_revision)
    attempt = writer.state["attempts"].get(task["current_attempt"])
    if not is_coordinator:
        if (writer.actor.member_id != task["owner_member_id"] or action not in {"progress", "submit", "block", "fail"}
                or task["state"] != "running" or attempt is None
                or attempt["driver_run_id"] != writer.actor.driver_run_id or attempt["generation"] != writer.actor.generation):
            raise TeamError("AUTHORITY_REVOKED", "A member can update only its current fenced attempt.", status=403)
    if action == "progress":
        result = await queue_message(writer, to_member_id=writer.run.root_session_id, body=summary,
            kind="progress", task_id=task_id, task_attempt_id=task["current_attempt"], from_member_id=task["owner_member_id"] if writer.actor.kind == "server" else None)
        return {"task": task, **result}
    if action in {"submit", "block", "fail"}:
        if task["state"] != "running" or attempt is None:
            raise TeamError("INVALID_TASK_TRANSITION", "There is no running attempt to complete.", current=task)
        if not summary.strip():
            raise TeamError("INVALID_TASK", "A result or blocking summary is required.", status=422)
        from team.paid_tools import pending_for_attempt
        unresolved = pending_for_attempt(writer.state, attempt["id"])
        if unresolved and action == "submit":
            raise TeamError("EXTERNAL_WORK_PENDING", "Paid jobs or their billing are still unresolved. Wait for their result before submitting this task.",
                current=[{"tool": row["tool"], "job_id": row.get("external_id")} for row in unresolved])
        if unresolved and action in {"block", "fail"}:
            raise TeamError("OUTCOME_UNKNOWN", "This attempt has unresolved paid work. Report the issue and wait for reconciliation; do not close or retry it.")
        if action == "submit" and task["output_schema"]:
            try:
                validate_structured_result(task["output_schema"], output)
            except SubagentCompositionError as exc:
                # Some compatible providers encode an object-valued tool
                # argument as JSON text. Accept decoding only when the declared
                # result schema also validates that object. Unstructured text
                # results and nested string fields retain their original type.
                try:
                    decoded = json.loads(output) if isinstance(output, str) else None
                    if not isinstance(decoded, dict):
                        raise ValueError("Expected an object")
                    validate_structured_result(task["output_schema"], decoded)
                except (ValueError, TypeError, SubagentCompositionError):
                    raise TeamError("INVALID_TASK_RESULT", str(exc), status=422) from exc
                output = decoded
        artifact_ids = []
        for artifact in artifacts or []:
            from db.models.file_asset import FileAsset
            from team.artifacts import require_owned, snapshot_key
            asset = await writer.db.get(FileAsset, artifact.get("file_asset_id"))
            require_owned(asset, writer.run)
            if asset.oss_key != snapshot_key(writer.run.workspace_id, writer.run.id, artifact.get("content_digest") or ""):
                raise TeamError("INVALID_ARTIFACT", "Artifact must reference a server-verified immutable snapshot.")
            saved = {**artifact, "id": ascending("tart"), "task_id": task_id, "attempt_id": attempt["id"], "member_id": task["owner_member_id"]}
            writer.append("team.artifact", "artifact", saved)
            artifact_ids.append(saved["id"])
        state = {"submit": "review", "block": "blocked", "fail": "failed"}[action]
        ended = {**attempt, "state": state, "summary": summary, "output": output, "artifact_ids": artifact_ids, "implicit": implicit, "ended_at": utcnow().isoformat()}
        writer.append("team.attempt", "attempt", ended)
        task = task_status(writer, task, state=state, blocked_reason=summary if action != "submit" else None)
        member_status(writer, task["owner_member_id"], current_attempt=None, execution_state="idle")
        if action == "submit" and not implicit and task["acceptance_mode"] == "auto" and not task["deliverable"]:
            writer.append("team.attempt", "attempt", {**ended, "state": "succeeded"})
            task = task_status(writer, task, state="succeeded")
        await queue_message(writer, to_member_id=writer.run.root_session_id, body=summary,
            kind="result", task_id=task_id, task_attempt_id=attempt["id"],
            from_member_id=task["owner_member_id"] if writer.actor.kind == "server" else None)
    else:
        require_coordinator(writer)
        if action == "edit":
            allowed = {"title", "description", "input_refs", "expected_output", "acceptance_criteria", "output_schema", "owner_member_id", "dependencies", "priority", "deliverable", "acceptance_mode", "write_scopes"}
            if task["state"] != "pending" or not changes or set(changes) - allowed:
                raise TeamError("INVALID_TASK", "Only pending task specifications can be edited.", current=task)
            if changes.get("deliverable", task["deliverable"]):
                changes = {**changes, "acceptance_mode": "coordinator"}
            task = task_status(writer, task, **changes)
        elif action == "accept":
            if task["state"] != "review":
                raise TeamError("INVALID_TASK_TRANSITION", "Only submitted results can be accepted.", current=task)
            writer.append("team.attempt", "attempt", {**attempt, "state": "succeeded"})
            task = task_status(writer, task, state="succeeded")
        elif action in {"retry", "rework", "reopen"}:
            if task["state"] == "outcome_unknown":
                raise TeamError("OUTCOME_UNKNOWN", "Reconcile the external outcome before any retry.")
            permitted = {"retry": {"blocked", "failed"}, "rework": {"review"}, "reopen": {"succeeded"}}
            if task["state"] not in permitted[action]:
                raise TeamError("INVALID_TASK_TRANSITION",
                    f"Action '{action}' requires task state {', '.join(sorted(permitted[action]))}; current state is '{task['state']}'. "
                    "Use accept/rework for review, retry for blocked/failed, or reopen for succeeded; do not repeat the same action.", current=task)
            if not (reason or "").strip():
                raise TeamError("INVALID_TASK", f"Action '{action}' requires a non-empty reason field; summary does not supply it. "
                    "Explain what changed before retrying, such as a confirmed grant or corrected input.", current=task, status=422)
            if any(t["state"] != "pending" and task_id in t["dependencies"] for t in writer.state["tasks"].values()):
                raise TeamError("DEPENDENCY_IN_USE", "An already dispatched downstream task uses this result.")
            task = task_status(writer, task, state="pending", current_attempt=None, blocked_reason=reason)
        elif action == "cancel":
            if not (reason or "").strip():
                raise TeamError("INVALID_TASK", "Canceling a task requires a non-empty reason field.", status=422)
            if task["state"] in {"running", "outcome_unknown"}:
                raise TeamError("MEMBER_BUSY", "Interrupt and reconcile the attempt before canceling its task.")
            task = task_status(writer, task, state="canceled", cancellation_reason=reason)
        else:
            raise TeamError("INVALID_TASK", "Unknown task action.", status=422)
    current = writer.state["attempts"].get(task.get("current_attempt"))
    return {"task": task, "artifact_ids": list(current.get("artifact_ids", [])) if current else []}


async def wait_member(writer: Writer, after_seq: int, timeout_seconds: int = 600) -> dict:
    require_open(writer)
    member_id = writer.actor.member_id
    if member_id not in writer.state["members"] or not 0 <= after_seq <= writer.state["seq"] or not 10 <= timeout_seconds <= 3600:
        raise TeamError("INVALID_WAIT", "Wait requires a member, a committed sequence and a 10–3600 second timeout.")
    inflight = any(a["state"] == "running" for a in writer.state["attempts"].values())
    inflight |= any(m["state"] == "queued" for m in writer.state["messages"].values())
    inflight |= any(r["state"] == "reserved" for r in writer.state["reservations"].values())
    if not inflight and not ready_tasks(writer.state):
        return {"status": "no_progress", "seq": writer.state["seq"], "reason": "No executable task, undelivered message or external operation can produce progress."}
    # A received message is a level-triggered wake; do not miss one that
    # committed between team_view and team_wait.
    unread = any(m["to_member_id"] == member_id and m["kind"] != "progress" and m["queued_seq"] > after_seq for m in writer.state["messages"].values())
    if unread:
        return {"status": "changed", "seq": writer.state["seq"]}
    member_status(writer, member_id, execution_state="waiting", last_seen_seq=after_seq,
        wait_after_seq=after_seq, wait_deadline=(utcnow() + timedelta(seconds=timeout_seconds)).isoformat())
    return {"status": "waiting", "seq": writer.state["seq"], "turn_yield": True}
