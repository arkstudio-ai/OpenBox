"""Read/mutation boundaries shared with ordinary Session and project paths."""
from sqlalchemy import select

from db.base import get_db_session
from db.models.session import Session
from db.models.team import TeamRun
from team.errors import TeamError


def check_trial_configuration(session, payload: dict) -> None:
    """A trial exercises one admitted version; HTTP cannot turn it into build."""
    if session.kind != "agent_trial":
        return
    for field in ("agent", "model", "variant", "video_model", "video_resolution"):
        if field in payload and payload[field] != getattr(session, field, None):
            raise TeamError("TRIAL_CONFIGURATION_FROZEN", "Start a new trial to use a different Agent version or model.",
                            current={"field": field, "value": getattr(session, field, None)})
    if payload.get("team_request") is not None or payload.get("format") is not None:
        raise TeamError("TRIAL_CONFIGURATION_FROZEN", "Trial capabilities and output format come from the saved Agent version.")


async def assert_no_active_project(project_id: str, *, db=None) -> None:
    if db is None:
        async with get_db_session() as connection:
            return await assert_no_active_project(project_id, db=connection)
    active = await db.scalar(select(TeamRun.id).where(TeamRun.project_id == project_id, TeamRun.project_active == 1).limit(1))
    if active:
        raise TeamError("TEAM_ACTIVE", "Pause is not sufficient: cancel or finish the active team before changing project history or deleting its project.", current={"team_run_id": active})


async def assert_session_history_mutable(session_id: str, user_id: str) -> None:
    async with get_db_session() as db:
        session = await db.get(Session, session_id)
        if session is None or session.user_id != user_id:
            return
        if session.kind == "team_member":
            raise TeamError("TEAM_MEMBER_READ_ONLY", "Team member transcripts are read-only.")
        await assert_no_active_project(session.project_id, db=db)


async def check_http_session(request, user: dict) -> None:
    """Keep workspace sharing from implicitly granting team access."""
    from fastapi import HTTPException
    session_id = request.path_params.get("session_id")
    if not session_id:
        return
    async with get_db_session() as db:
        session = await db.get(Session, session_id)
        if session is None:
            return
        if session.kind == "agent_trial":
            if session.user_id != user["user_id"] or session.workspace_id != user["workspace_id"]:
                raise HTTPException(404, "Session not found")
            if request.method in {"POST", "PATCH"}:
                try:
                    payload = await request.json()
                except ValueError:
                    payload = {}
                try:
                    if isinstance(payload, dict):
                        check_trial_configuration(session, payload)
                except TeamError as exc:
                    raise HTTPException(exc.status, exc.to_dict()) from exc
        if request.method == "POST" and ("/revert/" in request.url.path or request.url.path.endswith("/unrevert")):
            if session.user_id == user["user_id"] and session.workspace_id == user["workspace_id"]:
                try:
                    await assert_no_active_project(session.project_id, db=db)
                except TeamError as exc:
                    raise HTTPException(exc.status, exc.to_dict()) from exc
        run = await db.scalar(select(TeamRun).where(TeamRun.root_session_id == (session.parent_id if session.kind == "team_member" else session.id)).order_by(TeamRun.created_at.desc()).limit(1))
        if session.kind != "team_member" and run is None:
            return
        if session.user_id != user["user_id"] or session.workspace_id != user["workspace_id"]:
            raise HTTPException(404, "Session not found")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and session.kind == "team_member":
            raise HTTPException(409, {"code": "TEAM_MEMBER_READ_ONLY", "message": "Return to the team conversation to change its instructions."})
        if request.method == "DELETE" and run is not None and run.session_active == 1:
            raise HTTPException(409, {"code": "TEAM_ACTIVE", "message": "Cancel or finish the team before deleting this conversation."})
