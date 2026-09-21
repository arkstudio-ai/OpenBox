from types import SimpleNamespace

import pytest

from agent_catalog import repository
from agent_catalog.compiler import compile_agent
from agent_catalog.proposals import apply_answer
from db.base import get_db_session
from db.models.session import Session
from tests.unit.test_team_catalog import config, new_root
from tests.unit.test_team_compiler import spec


async def proposal(config, answer):
    root, actor = await new_root()
    definition = spec()
    compiled = compile_agent(definition, config=config, role="trial")
    created = await repository.create("agent", actor, "create", definition, capability_summary={"_compiled_trial": compiled.snapshot()})
    checkpoint = SimpleNamespace(user_id=actor.owner_user_id, status="answered", answers=[[answer]],
        continuation={"kind": "agent_proposal", "definition_id": created["id"], "workspace_id": actor.workspace_id,
            "revision": created["revision"], "version_id": created["version"]["id"], "content_digest": created["version"]["content_digest"]})
    return root, actor, created, checkpoint


@pytest.mark.parametrize("answer,status", [("启用", "active"), ("先存为草稿", "draft"), ("不要", "archived")])
async def test_agent_confirmation_applies_exact_saved_version(config, answer, status):
    root, actor, created, checkpoint = await proposal(config, answer)
    async with get_db_session() as db:
        session = await db.get(Session, root)
        await apply_answer(db, session, checkpoint, {})
    current = await repository.get("agent", created["id"], actor)
    assert current["status"] == status
    assert current["version"]["id"] == created["version"]["id"]


async def test_agent_confirmation_rejects_a_changed_draft(config):
    root, actor, created, checkpoint = await proposal(config, "启用")
    changed = spec()
    changed.instruction = "Different from the confirmation card"
    await repository.mutate("agent", created["id"], actor, "edit", "save_draft", 1, spec=changed)
    async with get_db_session() as db:
        with pytest.raises(ValueError, match="changed after"):
            await apply_answer(db, await db.get(Session, root), checkpoint, {})
    assert (await repository.get("agent", created["id"], actor))["status"] == "draft"


async def test_dismissed_agent_confirmation_keeps_draft(config):
    root, actor, created, checkpoint = await proposal(config, "启用")
    checkpoint.status = "rejected"
    async with get_db_session() as db:
        result, _ = await apply_answer(db, await db.get(Session, root), checkpoint, {})
    assert result["metadata"]["rejected"] is True
    assert (await repository.get("agent", created["id"], actor))["revision"] == 1


@pytest.mark.parametrize("kind,parent,agent", [("normal", "parent", "build"), ("team_member", "parent", "build"), ("cron", None, "build"), ("normal", None, "team"), ("normal", None, "plan")])
async def test_agent_management_is_interactive_build_only(config, monkeypatch, kind, parent, agent):
    from tool import agent_manage
    config.team_ui_enabled = True
    monkeypatch.setattr(agent_manage, "get_config", lambda: config)
    root, actor = await new_root()
    async with get_db_session() as db:
        session = await db.get(Session, root)
        session.kind, session.parent_id, session.agent = kind, parent, agent
    async def current():
        pass
    ctx = SimpleNamespace(session_id=root, user_id=actor.owner_user_id, workspace_id=actor.workspace_id, assert_run_current=current)
    result = await agent_manage.execute(agent_manage.AgentManageArgs(action="list"), ctx)
    assert result.metadata["code"] == "AUTHORITY_REVOKED"
