"""Workspace desktop exclusion uses the existing workspace row as its fence."""
from sqlalchemy import select

from db.models.team import TeamRun
from db.models.workspace import Workspace
from team.journal import read_state


def occupies_desktop(state):
    # Waiting releases a model execution slot, never the multi-step desktop
    # workflow. Unknown external effects retain ownership until reconciled.
    return any(attempt["state"] in {"running", "outcome_unknown"}
        and state["tasks"][attempt["task_id"]]["exclusive_group"] == "desktop"
        for attempt in state["attempts"].values())


async def other_desktop_busy(db, run, *, lock=False):
    if lock:
        await db.execute(select(Workspace.id).where(Workspace.id == run.workspace_id).with_for_update())
    others = (await db.execute(select(TeamRun).where(TeamRun.workspace_id == run.workspace_id,
        TeamRun.id != run.id, TeamRun.session_active == 1))).scalars().all()
    for other in others:
        if occupies_desktop(await read_state(db, other)):
            return True
    return False
