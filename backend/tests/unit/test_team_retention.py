from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from agent_catalog import repository
from agent_catalog.schemas import TeamSpec
from db.base import get_db_session
from db.models.session import Session
from db.models.team import TeamEvent, TeamRun
from session.session import delete_session
from team import commands
from team.errors import TeamError
from team.journal import command
from tests.unit.test_team_commands import setup_team
from tests.unit.test_team_compiler import spec


async def test_root_deletion_is_atomic_and_preserves_reusable_assets(monkeypatch):
    from sandbox import sandbox_manager
    monkeypatch.setattr(sandbox_manager, "release", AsyncMock())
    run_id, actor, server, root, members = await setup_team()
    agent = await repository.create("agent", actor, "keep-agent", spec())
    template = await repository.create("team", actor, "keep-template", TeamSpec(name="Reusable team"))
    with pytest.raises(TeamError, match="Cancel or finish"):
        await delete_session(root, actor.owner_user_id, actor.workspace_id)
    assert not await delete_session(root, "another-owner")
    async def close(writer):
        commands.run_status(writer, "canceling")
        commands.run_status(writer, "canceled")
        return {}
    await command(run_id, server, "cancel", {}, close)
    assert await delete_session(root, actor.owner_user_id, actor.workspace_id)
    async with get_db_session() as db:
        assert await db.get(TeamRun, run_id) is None
        assert not (await db.execute(select(TeamEvent.id).where(TeamEvent.team_run_id == run_id))).scalars().all()
        for sid in [root, *members]:
            assert (await db.get(Session, sid)).is_deleted
    assert (await repository.get("agent", agent["id"], actor))["id"] == agent["id"]
    assert (await repository.get("team", template["id"], actor))["id"] == template["id"]
    assert sandbox_manager.release.await_count == len(members) + 1
    assert await delete_session(root, actor.owner_user_id, actor.workspace_id)
