from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent_catalog import chat_creation, repository
from agent_catalog.compiler import compile_agent
from agent_catalog.proposals import apply_answer
from db.base import get_db_session
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from db.repository.preference_repo import PgPreferenceRepo
from team.errors import TeamError
from team.journal import Actor, utcnow
from tests.unit.test_team_catalog import config, new_root
from tests.unit.test_team_compiler import spec


@pytest.fixture
def creation_config(config, monkeypatch):
    from tool import agent_manage
    config.team_ui_enabled = True
    config.team_tools_enabled = True
    config.team_max_proposals_per_session = 4
    monkeypatch.setattr(agent_manage, "get_config", lambda: config)
    monkeypatch.setattr(chat_creation, "get_config", lambda: config)
    return config


async def context():
    root, actor = await new_root()
    async def current():
        pass
    return actor, SimpleNamespace(session_id=root, user_id=actor.owner_user_id, workspace_id=actor.workspace_id,
        part_id="create-part", message_id="create-message", sandbox=None, assert_run_current=current)


def materials(config, count=1):
    specs = [spec().model_copy(update={"name": f"Agent {index}"}) for index in range(count)]
    summaries = [{**(compiled := compile_agent(item, config=config, role="trial")).summary,
                  "_compiled_trial": compiled.snapshot()} for item in specs]
    return specs, summaries


async def test_creation_preference_is_strict_opt_in_and_preserves_other_preferences(creation_config):
    from auth.routes import PreferencesUpdate
    actor, ctx = await context()
    repo = PgPreferenceRepo()
    preference = await repo.upsert(actor.owner_user_id, extra={"mode": "dark"})
    assert preference["agent_autoapprove_t0"] is False
    saved, auto = await chat_creation.create_batch(actor, ctx, *materials(creation_config, 4))
    assert not auto and len(saved) == 4 and all(row["status"] == "draft" for row in saved)
    await repo.upsert(actor.owner_user_id, agent_autoapprove_t0=True)
    preference = await repo.get(actor.owner_user_id)
    assert preference["agent_autoapprove_t0"] is True and preference["extra"]["mode"] == "dark"
    for invalid in ("true", 1):
        with pytest.raises(ValidationError):
            PreferencesUpdate(agent_autoapprove_t0=invalid)


async def test_autoapproved_batch_replays_and_undo_is_scoped_reversible_and_idempotent(creation_config):
    actor, ctx = await context()
    await PgPreferenceRepo().upsert(actor.owner_user_id, agent_autoapprove_t0=True)
    inputs = materials(creation_config, 2)
    saved, auto = await chat_creation.create_batch(actor, ctx, *inputs)
    assert auto and all(row["status"] == "active" for row in saved)
    await PgPreferenceRepo().upsert(actor.owner_user_id, agent_autoapprove_t0=False)
    assert await chat_creation.create_batch(actor, ctx, *inputs) == (saved, True)
    first = saved[0]
    with pytest.raises(TeamError) as error:
        await chat_creation.undo_autoapproval(Actor("foreign", actor.workspace_id), first["id"], first["version"]["id"], 1, "undo")
    assert error.value.status == 404
    undone = await chat_creation.undo_autoapproval(actor, first["id"], first["version"]["id"], 1, "undo")
    assert undone["status"] == "draft" and undone["current_version_id"] is None
    assert undone["version"]["id"] == first["version"]["id"]
    assert await chat_creation.undo_autoapproval(actor, first["id"], first["version"]["id"], 1, "undo") == undone
    assert (await repository.get("agent", saved[1]["id"], actor))["status"] == "active"
    with pytest.raises(TeamError):
        await repository.get("agent", first["id"], actor, active_only=True)


@pytest.mark.parametrize("risk", ["desktop", "paid", "mcp"])
async def test_any_risky_definition_requires_confirmation_for_whole_batch(creation_config, risk):
    actor, ctx = await context()
    await PgPreferenceRepo().upsert(actor.owner_user_id, agent_autoapprove_t0=True)
    specs, _ = materials(creation_config, 2)
    if risk == "mcp":
        from agent_catalog.schemas import MCPRef
        specs[1].mcp_refs = [MCPRef(server="test-only", tools=["read_*"])]
    else:
        specs[1].tool_allowlist = ["computer" if risk == "desktop" else "image_gen"]
    summaries = [{"_compiled_trial": compile_agent(item, config=creation_config, role="trial").snapshot()} for item in specs]
    saved, auto = await chat_creation.create_batch(actor, ctx, specs, summaries)
    assert not auto and all(row["status"] == "draft" for row in saved)


async def test_name_conflict_rolls_back_entire_batch_and_limits_include_autoapproval(creation_config):
    actor, ctx = await context()
    specs, summaries = materials(creation_config, 2)
    prior = await repository.create("agent", actor, "existing", specs[1])
    with pytest.raises(TeamError, match="No new definitions"):
        await chat_creation.create_batch(actor, ctx, specs, summaries)
    assert [row["id"] for row in (await repository.list_definitions("agent", actor))["items"]] == [prior["id"]]
    creation_config.team_max_proposals_per_session = 1
    await PgPreferenceRepo().upsert(actor.owner_user_id, agent_autoapprove_t0=True)
    saved, auto = await chat_creation.create_batch(actor, ctx, specs[:1], summaries[:1])
    assert auto
    ctx.part_id = "another-part"
    specs[0].name = "Different name"
    with pytest.raises(TeamError) as error:
        await chat_creation.create_batch(actor, ctx, specs[:1], summaries[:1])
    assert error.value.code == "AGENT_PROPOSAL_LIMIT"


async def test_undo_refuses_a_later_user_edit(creation_config):
    actor, ctx = await context()
    await PgPreferenceRepo().upsert(actor.owner_user_id, agent_autoapprove_t0=True)
    specs, summaries = materials(creation_config)
    saved, _ = await chat_creation.create_batch(actor, ctx, specs, summaries)
    await repository.mutate("agent", saved[0]["id"], actor, "edit", "save_draft", 1, spec=specs[0])
    with pytest.raises(TeamError) as error:
        await chat_creation.undo_autoapproval(actor, saved[0]["id"], saved[0]["version"]["id"], 1, "undo")
    assert error.value.code == "STALE_REVISION"
    assert (await repository.get("agent", saved[0]["id"], actor))["revision"] == 2


async def test_discovery_requires_successful_server_tool_identity_in_same_session(creation_config):
    actor, ctx = await context()
    now = utcnow()
    async with get_db_session() as db:
        db.add(Message(id=ctx.message_id, session_id=ctx.session_id, user_id=ctx.user_id, role="assistant", created_at=now))
        await db.flush()
        db.add(Part(id=ctx.part_id, session_id=ctx.session_id, user_id=ctx.user_id, message_id=ctx.message_id,
                    type="tool", canonical_tool_id="read", data={"tool": "skill_search", "status": "completed"}, created_at=now))
    with pytest.raises(TeamError, match="Call skill_search"):
        await chat_creation.require_skill_discovery(ctx)
    async with get_db_session() as db:
        row = await db.get(Part, ctx.part_id)
        row.canonical_tool_id = "skill_search"
        row.data = {"tool": "skill_search", "status": "completed", "metadata": {"error": True}}
    with pytest.raises(TeamError):
        await chat_creation.require_skill_discovery(ctx)
    async with get_db_session() as db:
        (await db.get(Part, ctx.part_id)).data = {"tool": "skill_search", "status": "completed", "output": "No relevant Skills"}
    await chat_creation.require_skill_discovery(ctx)


async def test_batch_confirmation_validates_all_versions_before_publishing(creation_config):
    actor, ctx = await context()
    specs, summaries = materials(creation_config, 2)
    saved, _ = await chat_creation.create_batch(actor, ctx, specs, summaries)
    records = [{"definition_id": item["id"], "version_id": item["version"]["id"],
                "revision": 1, "content_digest": item["version"]["content_digest"]} for item in saved]
    checkpoint = SimpleNamespace(user_id=actor.owner_user_id, status="answered", answers=[["启用"], ["先存为草稿"]],
        continuation={"kind": "agent_proposal", "workspace_id": actor.workspace_id, "definitions": records})
    invalid = deepcopy(checkpoint)
    invalid.continuation["definitions"][1]["content_digest"] = "changed"
    async with get_db_session() as db:
        with pytest.raises(ValueError, match="changed after"):
            await apply_answer(db, await db.get(Session, ctx.session_id), invalid, {})
    assert all(item["status"] == "draft" for item in (await repository.list_definitions("agent", actor))["items"])
    async with get_db_session() as db:
        await apply_answer(db, await db.get(Session, ctx.session_id), checkpoint, {})
    assert (await repository.get("agent", saved[0]["id"], actor))["status"] == "active"
    assert (await repository.get("agent", saved[1]["id"], actor))["status"] == "draft"


async def test_invalid_batch_does_not_create_drafts_or_questions(creation_config, monkeypatch):
    from tool import agent_manage
    actor, ctx = await context()
    specs, summaries = materials(creation_config, 2)
    async def discovered(_):
        pass
    async def validate(item, *args, **kwargs):
        if item.name == specs[1].name:
            raise TeamError("INVALID_DEFINITION", "Second Agent is invalid")
        return summaries[0]
    monkeypatch.setattr(agent_manage, "require_skill_discovery", discovered)
    monkeypatch.setattr(agent_manage, "agent_summary", validate)
    result = await agent_manage.execute(agent_manage.AgentManageArgs(action="create", specs=specs), ctx)
    assert result.metadata["code"] == "INVALID_DEFINITION"
    assert (await repository.list_definitions("agent", actor))["items"] == []
    with pytest.raises(ValidationError):
        agent_manage.AgentManageArgs(action="create", specs=specs * 3)


async def test_tool_emits_four_server_built_questions_and_updates_still_require_confirmation(creation_config, monkeypatch):
    from tool import agent_manage
    from question import question
    actor, ctx = await context()
    specs, summaries = materials(creation_config, 4)
    asked = []
    class Suspended(Exception):
        pass
    async def discovered(_):
        pass
    async def validate(item, *args, **kwargs):
        return summaries[specs.index(item)]
    async def ask(*args, **kwargs):
        asked.append((args, kwargs))
        raise Suspended()
    monkeypatch.setattr(agent_manage, "require_skill_discovery", discovered)
    monkeypatch.setattr(agent_manage, "agent_summary", validate)
    monkeypatch.setattr(question, "ask", ask)
    with pytest.raises(Suspended):
        await agent_manage.execute(agent_manage.AgentManageArgs(action="create", specs=specs), ctx)
    args, kwargs = asked[-1]
    assert len(args[1]) == len(kwargs["continuation"]["definitions"]) == 4
    assert [item.detail["spec"]["name"] for item in args[1]] == [item.name for item in specs]
    assert all("_compiled_trial" not in item.detail["capability_summary"] for item in args[1])
    first = kwargs["continuation"]["definitions"][0]
    await PgPreferenceRepo().upsert(actor.owner_user_id, agent_autoapprove_t0=True)
    ctx.part_id = "update-part"
    with pytest.raises(Suspended):
        await agent_manage.execute(agent_manage.AgentManageArgs(action="update", definition_id=first["definition_id"],
            expected_revision=1, spec=specs[0]), ctx)
    assert len(asked[-1][0][1]) == 1
    assert asked[-1][0][1][0].detail["previous_spec"]["name"] == specs[0].name
