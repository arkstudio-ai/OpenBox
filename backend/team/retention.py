"""Team history follows the existing root-session deletion transaction."""
from sqlalchemy import delete, select, update

from db.models.agent_inbox import AgentInboxItem
from db.models.session import Session
from db.models.team import TeamEvent, TeamRun
from team.errors import TeamError


async def delete_with_root_locked(db, root: Session, now) -> list[str]:
    """The caller owns/locks root; reusable definitions are never touched."""
    runs = (await db.execute(select(TeamRun).where(TeamRun.root_session_id == root.id,
        TeamRun.owner_user_id == root.user_id, TeamRun.workspace_id == root.workspace_id).with_for_update())).scalars().all()
    if any(run.session_active is not None for run in runs):
        raise TeamError("TEAM_ACTIVE", "Cancel or finish the active team before deleting its conversation.")
    if not runs:
        return []
    members = (await db.execute(select(Session).where(Session.parent_id == root.id, Session.kind == "team_member",
        Session.user_id == root.user_id, Session.workspace_id == root.workspace_id).order_by(Session.id).with_for_update())).scalars().all()
    from session.internal_parts import clear_internal_session_locked
    from trajectory import emit_control
    from trajectory.types import iso
    for member in members:
        await clear_internal_session_locked(db, member)
        member.is_deleted, member.deleted_at, member.updated_at = True, now, now
        member.status = "idle"
        emit_control({"type": "session.deleted", "session_id": member.id, "user_id": root.user_id,
                      "deleted_at": iso(now)}, db=db)
    member_ids = [member.id for member in members]
    if member_ids:
        await db.execute(update(AgentInboxItem).where(AgentInboxItem.session_id.in_(member_ids),
            AgentInboxItem.state == "accepted").values(state="canceled", canceled_at=now, updated_at=now))
        await db.execute(update(AgentInboxItem).where(AgentInboxItem.session_id.in_(member_ids),
            AgentInboxItem.state == "claimed").values(state="settled", outcome="canceled", settled_at=now,
                claim_expires_at=None, updated_at=now))
    run_ids = [run.id for run in runs]
    await db.execute(delete(TeamEvent).where(TeamEvent.team_run_id.in_(run_ids)))
    await db.execute(delete(TeamRun).where(TeamRun.id.in_(run_ids)))
    return member_ids
