"""Public projections exclude authority and keep snapshot responses bounded."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy

from sqlalchemy import select

from db.base import get_db_session
from db.models.team import TeamEvent
from team.errors import TeamError
from team.journal import Actor, event_dict, owned_run, read_state

TASK_FIELDS = {"id", "title", "state", "revision", "owner_member_id", "dependencies", "priority", "deliverable", "acceptance_mode", "current_attempt", "blocked_reason"}
MEMBER_FIELDS = {"id", "alias", "role", "source", "name", "description", "responsibility", "model", "tool_ids", "skill_refs", "definition_id", "version_id", "exclusive_group", "membership_state", "execution_state", "current_attempt", "wait_after_seq", "wait_deadline", "error", "admission_seq", "display"}


def public_state(state: dict) -> dict:
    from team.amendments import GRANT_FIELDS
    coordinator = state["run"]["root_session_id"]
    delegated = Counter(t["owner_member_id"] for t in state["tasks"].values() if t["current_attempt"])
    messages = Counter(tuple(sorted([m["from_member_id"], m["to_member_id"]])) for m in state["messages"].values() if m["kind"] != "progress")
    active_attempts = {member["current_attempt"] for member in state["members"].values() if member.get("current_attempt")}
    tasks = list(state["tasks"].values())
    visible_tasks = sorted(tasks, key=lambda task: task.get("current_attempt") not in active_attempts)[:200]
    policy = {**state["policy"], **{key: state["grant"][key] for key in GRANT_FIELDS if key in state["grant"]}}
    from agent_catalog.schemas import TeamPolicy
    policy = TeamPolicy.model_validate(policy).model_dump(mode="json")
    run_info = {key: deepcopy(value) for key, value in state["run"].items() if key != "budget_check"}
    return {"id": state["id"], "seq": state["seq"], "run": run_info, "policy": policy,
        "members": [{key: value for key, value in member.items() if key in MEMBER_FIELDS} for member in state["members"].values()],
        "tasks": [{key: value for key, value in task.items() if key in TASK_FIELDS} for task in visible_tasks],
        "task_count": len(tasks), "completed_task_count": sum(task["state"] == "succeeded" for task in tasks), "artifact_count": len(state["artifacts"]),
        "links": [{"from": coordinator, "to": member, "kind": "task", "count": count} for member, count in delegated.items()]
            + [{"from": pair[0], "to": pair[1], "kind": "message", "count": count} for pair, count in messages.items()],
        "notices": deepcopy(state["notices"][-20:])}


def public_event(event: dict) -> dict:
    event = deepcopy(event)
    event.pop("actor_member_id", None)
    data = event["payload"]["data"]
    if event["kind"] == "team.member.admitted":
        data = {key: value for key, value in data.items() if key in MEMBER_FIELDS}
    if event["kind"] == "team.run.created":
        data.pop("grant_snapshot", None)
    # A command response may contain an internal receipt or resolved authority.
    event["payload"] = {"schema_version": 1, "data": data}
    return event


async def events(run_id: str, actor: Actor, *, after_seq: int, limit: int = 200) -> dict:
    async with get_db_session() as db:
        run = await owned_run(db, run_id, actor)
        if not 0 <= after_seq <= run.last_seq:
            raise TeamError("INVALID_EVENT_WATERMARK", "Reload the team snapshot before catching up.", current={"seq": run.last_seq}, status=409)
        limit = max(1, min(limit, 500))
        rows = (await db.execute(select(TeamEvent).where(TeamEvent.team_run_id == run_id, TeamEvent.sequence > after_seq)
            .order_by(TeamEvent.sequence).limit(limit))).scalars().all()
        next_seq = rows[-1].sequence if rows else after_seq
        return {"events": [public_event(event_dict(row)) for row in rows], "seq": next_seq, "last_seq": run.last_seq, "has_more": next_seq < run.last_seq}


async def collection(run_id: str, actor: Actor, kind: str, *, offset: int = 0, limit: int = 50,
                     member: str | None = None, between: tuple[str, str] | None = None, task_id: str | None = None) -> dict:
    if kind not in {"members", "tasks", "messages", "artifacts", "attempts"}:
        raise TeamError("INVALID_COLLECTION", "Unknown team detail collection.", status=404)
    async with get_db_session() as db:
        run = await owned_run(db, run_id, actor)
        state = await read_state(db, run)
        entries = list(state[kind].values())
        if task_id is not None:
            if kind not in {"tasks", "attempts", "artifacts"}:
                raise TeamError("INVALID_COLLECTION_FILTER", "Task filtering applies to tasks, attempts or artifacts.", status=422)
            entries = [item for item in entries if item.get("id" if kind == "tasks" else "task_id") == task_id]
        if kind == "messages":
            if member:
                entries = [item for item in entries if member in {item["from_member_id"], item["to_member_id"]}]
            if between:
                entries = [item for item in entries if {item["from_member_id"], item["to_member_id"]} == set(between)]
        total = len(entries)
        entries = entries[max(0, offset):max(0, offset) + max(1, min(limit, 100))]
        if kind == "messages" and entries:
            rows = (await db.execute(select(TeamEvent).where(TeamEvent.team_run_id == run_id, TeamEvent.sequence.in_([item["queued_seq"] for item in entries])))).scalars().all()
            bodies = {row.entity_id: row.payload["data"] for row in rows}
            entries = [{**bodies[item["id"]], **item} for item in entries]
        if kind == "artifacts" and entries:
            from db.models.file_asset import FileAsset
            assets = (await db.execute(select(FileAsset).where(FileAsset.id.in_([item.get("file_asset_id") for item in entries]),
                FileAsset.user_id == actor.owner_user_id, FileAsset.workspace_id == actor.workspace_id,
                FileAsset.project_id == run.project_id, FileAsset.is_deleted.is_(False), FileAsset.status == "ready"))).scalars().all()
            index = {asset.id: {"id": asset.id, "name": asset.name, "mime": asset.mime, "size": asset.size} for asset in assets}
            entries = [{**item, "asset": index.get(item.get("file_asset_id"))} for item in entries]
        return {"items": deepcopy(entries), "total": total, "next_offset": offset + len(entries) if offset + len(entries) < total else None, "seq": state["seq"]}
