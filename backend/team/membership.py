"""Retirement preserves history and requires resolved execution first."""
from sqlalchemy import select, update

from db.models.agent_driver import AgentDriverState
from db.models.agent_inbox import AgentInboxItem
from team.commands import member_status, require_coordinator, require_open
from team.errors import TeamError
from team.journal import utcnow


async def retire_member(writer, member_id: str, reason: str):
    require_coordinator(writer)
    require_open(writer)
    member = writer.state["members"].get(member_id)
    if member is None or member["role"] != "member":
        raise TeamError("TEAM_MEMBER_NOT_FOUND", "Choose a work member from this run.", status=404)
    if member["membership_state"] == "retired":
        return {"status": "retired", "member_id": member_id}
    if member["membership_state"] != "active":
        raise TeamError("INVALID_MEMBER_TRANSITION", "Only an active member can retire.")
    driver = await writer.db.get(AgentDriverState, member_id, with_for_update=True)
    if driver and driver.phase != "idle":
        raise TeamError("MEMBER_BUSY", "Interrupt the member and wait for its Driver to stop before retiring it.")
    attempts = [a for a in writer.state["attempts"].values() if a["member_id"] == member_id and a["state"] in {"running", "outcome_unknown"}]
    if attempts:
        raise TeamError("OUTCOME_UNKNOWN", "Resolve running or unknown attempts before retiring this member.", current=[a["id"] for a in attempts])
    tasks = [task for task in writer.state["tasks"].values() if task["owner_member_id"] == member_id and task["state"] not in {"succeeded", "canceled", "review"}]
    if tasks:
        raise TeamError("MEMBER_BUSY", "Reassign pending work or explicitly cancel it before retiring its owner.", current=[t["id"] for t in tasks])
    claimed = await writer.db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.session_id == member_id,
        AgentInboxItem.user_id == writer.run.owner_user_id, AgentInboxItem.state == "claimed").limit(1))
    if claimed:
        raise TeamError("MEMBER_BUSY", "Wait for claimed Inbox work to settle before retirement.")
    now = utcnow()
    await writer.db.execute(update(AgentInboxItem).where(AgentInboxItem.session_id == member_id,
        AgentInboxItem.user_id == writer.run.owner_user_id, AgentInboxItem.source_type.is_not(None),
        AgentInboxItem.state == "accepted").values(state="canceled", canceled_at=now, updated_at=now, outcome="canceled"))
    for message in list(writer.state["messages"].values()):
        if message["state"] == "queued" and message["to_member_id"] == member_id:
            writer.append("team.message.canceled", "message", {"id": message["id"], "to_member_id": member_id})
    member_status(writer, member_id, membership_state="retired", execution_state="stopped", error=reason,
        wait_after_seq=None, wait_deadline=None, current_attempt=None, interrupt_requested=False)
    return {"status": "retired", "member_id": member_id, "name": member.get("name") or member["alias"], "changed": True}
