"""Deterministic progress checks, independent of the coordinator's wording."""
from datetime import datetime, timezone

from sqlalchemy import JSON, func, or_, select, type_coerce

from db.models.agent_inbox import AgentInboxItem
from db.models.question import QuestionCheckpoint
from db.models.team import TeamEvent
from team.journal import digest, utcnow
from team.state import ready_tasks


def aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def candidates(db, state, drivers) -> list[dict]:
    from core.config import get_config
    if state["run"]["state"] not in {"running", "waiting"}:
        return []
    root = state["run"]["root_session_id"]
    # A user question is an intentional wait, including a saved answer whose
    # continuation has not applied yet. It cannot be called a team stall.
    if await db.scalar(select(QuestionCheckpoint.id).where(QuestionCheckpoint.session_id == root,
            QuestionCheckpoint.applied.is_(False), QuestionCheckpoint.status.in_(["pending", "answered", "rejected"])).limit(1)):
        return []
    task_state = type_coerce(TeamEvent.payload, JSON)["data"]["state"].as_string()
    changes = select(TeamEvent.sequence, TeamEvent.created_at, TeamEvent.kind, TeamEvent.entity_id,
        task_state.label("task_state"), func.lag(task_state).over(partition_by=TeamEvent.entity_id,
            order_by=TeamEvent.sequence).label("previous_state")).where(TeamEvent.team_run_id == state["id"],
                TeamEvent.kind.in_(["team.task", "team.artifact"])).subquery()
    progress_rows = (await db.execute(select(changes.c.kind, changes.c.entity_id,
        func.max(changes.c.sequence).label("sequence"), func.max(changes.c.created_at).label("created_at")).where(or_(
        changes.c.kind == "team.artifact", changes.c.previous_state.is_(None),
        changes.c.task_state != changes.c.previous_state)).group_by(changes.c.kind, changes.c.entity_id))).all()
    progress = max(progress_rows, key=lambda row: row.sequence, default=None)
    task_progress = {row.entity_id: row.sequence for row in progress_rows if row.kind == "team.task"}
    progress_seq = progress.sequence if progress else 0
    progress_at = aware(progress.created_at if progress else state["run"]["created_at"])
    found = []
    live_members = [mid for mid, driver in drivers.items() if driver.live and mid != root]
    if live_members and (utcnow() - progress_at).total_seconds() >= get_config().team_stall_seconds:
        prior = state["run"].get("stall_progress_seq") == progress_seq
        count = state["run"].get("stall_count", 0) if prior else 0
        previous = state["run"].get("stall_notified_at")
        if not prior or not previous or (utcnow() - aware(previous)).total_seconds() >= get_config().team_stall_seconds:
            found.append({"code": "TEAM_PROGRESS_STALLED", "progress_seq": progress_seq,
                "count": count + 1, "members": sorted(live_members),
                "message": "No task or artifact progress within the configured interval; inspect the listed members."})
    pending = bool(await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.session_id.in_(state["members"]),
        AgentInboxItem.state.in_(["accepted", "claimed"])).limit(1)))
    pending |= any(row["state"] == "queued" and row["kind"] != "progress" for row in state["messages"].values())
    unresolved = any(row["state"] == "reserved" for row in state["reservations"].values())
    unresolved |= any(row["state"] == "outcome_unknown" for row in state["attempts"].values())
    unfinished = [task["id"] for task in state["tasks"].values() if task["deliverable"] and task["state"] not in {"succeeded", "canceled"}]
    busy = any(driver.phase != "idle" for driver in drivers.values())
    provisioning = any(member["membership_state"] == "provisioning" for member in state["members"].values())
    if unfinished and not (busy or provisioning or pending or unresolved or ready_tasks(state)):
        found.append({"code": "TEAM_NO_EXECUTABLE_WORK", "progress_seq": progress_seq, "tasks": unfinished,
            "message": "Required deliverables remain, but no execution, pending input or external work can advance them."})
    blocked = [task["id"] for task in state["tasks"].values() if task["state"] == "pending"
        and any(state["tasks"][dep]["state"] in {"failed", "canceled", "outcome_unknown"} for dep in task["dependencies"])]
    if blocked:
        found.append({"code": "TEAM_DEPENDENCY_BLOCKED", "progress_seq": progress_seq, "tasks": blocked,
            "message": "Upstream failures prevent these tasks from starting; review retry, replacement or cancellation."})
    pairs = {}
    for message in state["messages"].values():
        task_id = message.get("task_id")
        if message["kind"] in {"progress", "control"} or message["queued_seq"] <= task_progress.get(task_id, 0):
            continue
        pair = tuple(sorted((message["from_member_id"], message["to_member_id"])))
        key = (*pair, task_id)
        pairs.setdefault(key, []).append(message)
    for key, messages in pairs.items():
        if len(messages) >= 6 and len({item["from_member_id"] for item in messages}) > 1:
            found.append({"code": "TEAM_REPEATED_EXCHANGE", "progress_seq": task_progress.get(key[2], 0),
                "members": list(key[:2]), "task_id": key[2],
                "message": "Members exchanged at least six messages without task progress; check for duplicated work."})
    for item in found:
        item["id"] = "live_" + digest(item)[:48]
    if not found:
        return []
    emitted = set((await db.scalars(select(TeamEvent.entity_id).where(TeamEvent.team_run_id == state["id"],
        TeamEvent.kind == "team.notice", TeamEvent.entity_id.in_([item["id"] for item in found])))).all())
    return [item for item in found if item["id"] not in emitted]


async def advance(writer, drivers) -> set[str]:
    from team.commands import member_status, run_status
    from team.runtime import runtime
    root = writer.run.root_session_id
    found = await candidates(writer.db, writer.state, drivers)
    if not found:
        return set()
    for item in found:
        writer.append("team.notice", "notice", item)
        if item["code"] == "TEAM_PROGRESS_STALLED":
            run_status(writer, "pausing" if item["count"] >= 2 else writer.state["run"]["state"],
                pause_reason="stalled" if item["count"] >= 2 else writer.state["run"].get("pause_reason"),
                stall_progress_seq=item["progress_seq"], stall_count=item["count"], stall_notified_at=utcnow().isoformat())
        elif item["code"] == "TEAM_NO_EXECUTABLE_WORK":
            member_status(writer, root, nudged=True)
    if writer.state["run"]["state"] == "pausing":
        return set()
    await runtime.enqueue(writer.db, writer.run, root, {"kind": "team_control", "after_seq": writer.state["seq"]},
        "team:liveness:" + digest([item["id"] for item in found])[:48],
        "团队需要处理：" + "; ".join(item["message"] for item in found)
        + " 请查看 team_view 中的注意事项及任务，处理实际阻塞，不要重复未知外部操作或用报告替代交付。")
    member_status(writer, root, execution_state="queued", wait_after_seq=None, wait_deadline=None)
    return {root}
