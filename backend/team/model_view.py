"""Bounded journal reads for the coordinator and independent work members."""
from copy import deepcopy

from sqlalchemy import select

from db.models.team import TeamEvent
from team.errors import TeamError
from team.projection import TASK_FIELDS, public_state


async def view(writer, *, section: str, offset: int, limit: int, task_id: str | None) -> dict:
    state = writer.state
    member_id = writer.actor.member_id
    coordinator = member_id == writer.run.root_session_id
    if task_id is not None and task_id not in state["tasks"]:
        raise TeamError("TEAM_TASK_NOT_FOUND", "Task does not belong to this run.", status=404)
    if section == "summary":
        public = public_state(state)
        # Filter before paginating; the UI projection's first 200 tasks may
        # otherwise hide this member's current work in a long-running team.
        tasks = list(state["tasks"].values())
        if not coordinator:
            own = [task for task in tasks if task["owner_member_id"] == member_id]
            relevant = {task["id"] for task in own} | {dep for task in own for dep in task["dependencies"]}
            tasks = [task for task in tasks if task["id"] in relevant]
        if task_id is not None:
            tasks = [task for task in tasks if task["id"] == task_id]
        public["task_next_offset"] = offset + limit if offset + limit < len(tasks) else None
        public["tasks"] = [{key: deepcopy(value) for key, value in task.items() if key in TASK_FIELDS}
                           for task in tasks[offset:offset + limit]]
        public["task_details"] = "Use section=tasks with offset/limit or task_id for full task details."
        public["artifacts"] = deepcopy(list(state["artifacts"].values())[-limit:])
        public.pop("links", None)
        return public
    if task_id is not None and section == "messages":
        entries = [entry for entry in state[section].values() if entry.get("task_id") == task_id]
    else:
        entries = [entry for entry in state[section].values()
            if task_id is None or entry.get("id" if section == "tasks" else "task_id") == task_id]
    if section == "messages" and not coordinator:
        entries = [entry for entry in entries if member_id in {entry["from_member_id"], entry["to_member_id"]}]
    total = len(entries)
    entries = entries[offset:offset + limit]
    if section == "messages" and entries:
        rows = (await writer.db.scalars(select(TeamEvent).where(TeamEvent.team_run_id == writer.run.id,
            TeamEvent.sequence.in_([entry["queued_seq"] for entry in entries])))).all()
        bodies = {row.entity_id: row.payload["data"] for row in rows}
        entries = [{**bodies[entry["id"]], **entry} for entry in entries]
    return {"section": section, "items": deepcopy(entries), "total": total,
        "next_offset": offset + len(entries) if offset + len(entries) < total else None, "seq": state["seq"]}
