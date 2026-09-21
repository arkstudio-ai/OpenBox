"""Durable Driver backpressure shared by fast and generic Inbox recovery."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from db.base import get_db_session
from db.models.session import Session
from db.models.team import TeamRun
from team.commands import run_status
from team.journal import Actor, command, snapshot, utcnow


def deferred(state: dict) -> bool:
    value = state["run"].get("capacity_retry_at")
    if not value:
        return False
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when > utcnow()


async def record(session_id: str, user_id: str) -> None:
    """A quota refusal preserves the original input and attempt identity."""
    async with get_db_session() as db:
        session = await db.get(Session, session_id)
        if session is None or session.user_id != user_id:
            return
        root = session.parent_id if session.kind == "team_member" else session.id
        run = await db.scalar(select(TeamRun).where(TeamRun.root_session_id == root,
            TeamRun.owner_user_id == user_id, TeamRun.session_active == 1))
        if run is None:
            return
        actor = Actor(user_id, run.workspace_id, "server")
        run_id = run.id
    state = await snapshot(run_id, actor)
    if deferred(state) or state["run"]["state"] not in {"running", "waiting"}:
        return
    async def postpone(writer):
        if deferred(writer.state) or writer.state["run"]["state"] not in {"running", "waiting"}:
            return {"deferred": False}
        failures = writer.state["run"].get("capacity_failures", 0) + 1
        seconds = min(10 * 2 ** min(failures - 1, 5), 300)
        retry_at = (utcnow() + timedelta(seconds=seconds)).isoformat()
        run_status(writer, writer.state["run"]["state"], capacity_failures=failures,
            capacity_retry_at=retry_at, capacity_member_id=session_id)
        writer.append("team.notice", "notice", {"id": f"capacity:{writer.state['seq']}",
            "code": "DRIVER_BACKPRESSURE", "member_id": session_id, "retry_at": retry_at,
            "message": "Waiting for an execution slot; accepted work remains queued."})
        return {"deferred": True, "seq": writer.state["seq"]}
    result = await command(run_id, actor, f"capacity:{state['seq']}", {}, postpone)
    if result["deferred"]:
        from bus import bus
        bus.publish("team.run.updated", {"userId": user_id, "sessionId": root,
            "teamRunId": run_id, "seq": result["seq"], "state": state["run"]["state"]})


def clear(writer) -> None:
    if writer.state["run"].get("capacity_retry_at"):
        run_status(writer, writer.state["run"]["state"], capacity_failures=0,
            capacity_retry_at=None, capacity_member_id=None)
