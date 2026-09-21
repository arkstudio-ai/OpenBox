"""Existing member contexts cannot keep using revoked live authority."""
import pytest

from agent.hooks import ToolHooks
from agent.subagent_authority import SubagentAuthorityError
from db.base import get_db_session
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from team import commands, runtime_binding
from team.journal import command
from tests.unit.test_team_commands import setup_team
from tests.unit.test_subagent_composition import _config
from tool.tool import ToolContext


@pytest.mark.parametrize("target", ["user", "workspace", "membership", "project", "retired"])
async def test_live_revocation_stops_frozen_member(target, monkeypatch):
    run, actor, server, root, members = await setup_team()
    monkeypatch.setattr(runtime_binding, "get_config", lambda: _config("openai/test"))
    async with get_db_session() as db:
        session = await db.get(Session, members[0])
    await runtime_binding.load_binding(session)
    assert await runtime_binding.current_grant()
    if target == "retired":
        async def retire(writer):
            commands.member_status(writer, members[0], membership_state="retired")
            return {"retired": True}
        await command(run, server, "retired", {}, retire)
    else:
        async with get_db_session() as db:
            if target == "user":
                (await db.get(User, actor.owner_user_id)).is_active = False
            elif target == "workspace":
                (await db.get(Workspace, actor.workspace_id)).is_deleted = True
            elif target == "membership":
                (await db.get(WorkspaceMember, (actor.workspace_id, actor.owner_user_id))).status = "removed"
            else:
                (await db.get(Project, session.project_id)).is_deleted = True
    with pytest.raises(SubagentAuthorityError):
        await runtime_binding.current_grant()
    # This check is after model completion, not merely at the next step's
    # catalogue resolution. No old ToolInfo can execute under a revoked grant.
    hooks = ToolHooks(members[0], actor.owner_user_id)
    ctx = ToolContext(session_id=members[0], user_id=actor.owner_user_id,
        workspace_id=actor.workspace_id, project_id=session.project_id)
    blocked = await hooks.authorize_tool("read", {"file_path": "report.txt"}, ctx=ctx)
    assert blocked.metadata["code"] == "AUTHORITY_REVOKED"
