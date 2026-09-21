"""Trusted cost attribution captured when work starts, never inferred from text."""
from __future__ import annotations

CATEGORIES = ("coordinator", "member_work", "rework")


def attribution(state: dict, member_id: str) -> dict:
    member = state["members"][member_id]
    attempt = state["attempts"].get(member.get("current_attempt"))
    repeated = bool(member.get("nudged") or member.get("consecutive_failures")
        or (attempt and attempt["number"] > 1))
    return {"schema_version": 1, "run_id": state["id"], "member_id": member_id,
        "category": "rework" if repeated else "coordinator" if member["role"] == "coordinator" else "member_work",
        "attempt_id": attempt["id"] if attempt else None,
        "attempt_number": attempt["number"] if attempt else None,
        "nudged": bool(member.get("nudged")), "as_of_seq": state["seq"]}


async def request_attribution(db, session) -> dict | None:
    from db.models.team import TeamRun
    from team.journal import read_state
    from team.runtime_binding import current_binding
    binding = current_binding()
    if not binding or not binding.run_id or binding.member_id != session.id:
        return None
    run = await db.get(TeamRun, binding.run_id)
    if not run or run.owner_user_id != session.user_id or run.workspace_id != session.workspace_id:
        return None
    state = await read_state(db, run)
    if session.id not in state["members"]:
        return None
    return attribution(state, session.id)
