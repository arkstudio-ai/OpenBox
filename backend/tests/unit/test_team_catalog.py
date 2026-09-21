from dataclasses import replace
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from agent_catalog import repository
from agent_catalog.schemas import AgentSpec, TeamSpec
from db.base import get_db_session
from db.models.project import Project
from db.models.session import Session
from db.models.team import AgentDefinitionVersion, TeamRun, TeamEvent
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from team.errors import TeamError
from team.journal import Actor, utcnow, snapshot
from tests.unit.test_subagent_composition import _config
from tests.unit.test_team_compiler import spec


@pytest.fixture
def config(monkeypatch):
    from core import config as core_config
    from agent_catalog import catalog
    selected = _config("openai/test")
    selected.agent = {}
    selected.team_admission_enabled = True
    selected.team_generated_members_enabled = True
    selected.team_max_definitions = 100
    selected.team_tools_enabled = False
    monkeypatch.setattr(core_config, "get_config", lambda: selected)
    monkeypatch.setattr(catalog, "get_config", lambda: selected)
    return selected


async def new_root():
    suffix = uuid.uuid4().hex[:16]
    now = utcnow()
    async with get_db_session() as db:
        user = User(id=f"user-{suffix}", username=f"test-{suffix}", created_at=now, updated_at=now)
        workspace_id = f"workspace-{suffix}"
        db.add(user)
        await db.flush()
        db.add(Workspace(id=workspace_id, owner_user_id=user.id, name="Team tests", created_at=now, updated_at=now))
        await db.flush()
        db.add(WorkspaceMember(workspace_id=workspace_id, user_id=user.id, role="owner", status="active", created_at=now, updated_at=now))
        project = Project(id=f"project-{suffix}", user_id=user.id, workspace_id=workspace_id, name="Team tests", slug=suffix, created_at=now, updated_at=now)
        root = Session(id=f"session-{suffix}", user_id=user.id, workspace_id=workspace_id, project_id=project.id, agent="build", status="idle", kind="normal", created_at=now, updated_at=now)
        db.add_all([user, project, root])
        await db.flush()
        actor = Actor(user.id, root.workspace_id)
    return root.id, actor


async def test_published_versions_stay_immutable_while_drafts_change(config):
    _, actor = await new_root()
    created = await repository.create("agent", actor, "create", spec())
    assert await repository.create("agent", actor, "create", spec()) == created
    active = await repository.mutate("agent", created["id"], actor, "publish", "publish", 1)
    edited = spec()
    edited.instruction = "A new draft instruction"
    draft = await repository.mutate("agent", created["id"], actor, "draft", "save_draft", 2, spec=edited)
    assert draft["status"] == "active"
    assert draft["current_version_id"] == active["current_version_id"]
    current = await repository.get("agent", created["id"], actor, active_only=True)
    assert current["version"]["spec"]["instruction"] != edited.instruction
    listed = (await repository.list_definitions("agent", actor, status="active"))["items"]
    assert listed[0]["version"]["id"] == current["version"]["id"]
    with pytest.raises(TeamError) as error:
        await repository.get("agent", created["id"], actor, active_only=True, version_id=draft["version"]["id"])
    assert error.value.code == "AGENT_NOT_ACCESSIBLE"
    published = await repository.mutate("agent", created["id"], actor, "publish-v2", "publish", 3)
    assert published["version"]["version"] == 2 and published["draft_version_id"] is None
    history = await repository.versions("agent", created["id"], actor)
    assert [row["version"] for row in history["items"]] == [2, 1]
    assert all(row["published"] for row in history["items"])
    assert (await repository.get("agent", created["id"], actor, active_only=True, version_id=current["version"]["id"]))["version"] == current["version"]


async def test_template_rows_count_only_owned_runs_and_resolve_pinned_member_icons(config):
    from tests.unit.test_team_journal import seed_run
    _, actor = await new_root()
    _, outsider = await new_root()
    first_spec = AgentSpec.model_validate({**spec().model_dump(), "display": {"icon": "🔎", "color": "green"}})
    saved = await repository.create("agent", actor, "icon-role", first_spec)
    published = await repository.mutate("agent", saved["id"], actor, "publish-icon", "publish", 1)
    revised = AgentSpec.model_validate({**first_spec.model_dump(), "display": {"icon": "✅", "color": "green"}})
    await repository.mutate("agent", saved["id"], actor, "edit-icon", "save_draft", 2, spec=revised)
    await repository.mutate("agent", saved["id"], actor, "publish-icon-2", "publish", 3)
    private = await repository.create("agent", outsider, "private", spec())
    template = await repository.create("team", actor, "row-template", TeamSpec(name="Versioned member previews", preset_members=[
        {"alias": "pinned", "agent_ref": saved["id"], "version_id": published["current_version_id"], "version_policy": "pinned"},
        {"alias": "current", "agent_ref": saved["id"]},
        {"alias": "inaccessible", "agent_ref": private["id"]},
        {"alias": "disabled", "inline": first_spec, "enabled": False},
    ]))
    own_run, _, _ = await seed_run(scope=actor)
    foreign_run, _, _ = await seed_run(scope=outsider)
    async with get_db_session() as db:
        for identifier in (own_run, foreign_run):
            (await db.get(TeamRun, identifier)).template_id = template["id"]
    row = (await repository.list_definitions("team", actor))["items"][0]
    assert row["run_count"] == 1
    assert [(entry["alias"], entry["display"]) for entry in row["member_previews"]] == [
        ("pinned", {"icon": "🔎", "color": "green"}), ("current", {"icon": "✅", "color": "green"}), ("inaccessible", None)]
    assert row["member_previews"][-1]["name"] == "inaccessible"
    assert all(set(entry) == {"alias", "name", "display"} for entry in row["member_previews"])


async def test_provider_drift_is_permanent_proposal_error_not_endless_retry(config):
    from agent_catalog.catalog import prepare_lineup, restore_lineup
    _, actor = await new_root()
    proposal = await prepare_lineup(TeamSpec(name="Team"), actor)
    config.provider["openai"]["subagent_capabilities"] = ["model", "tool_filter"]
    with pytest.raises(TeamError) as error:
        restore_lineup(proposal.saved())
    assert error.value.code == "CAPABILITY_UNSUPPORTED"
    assert error.value.status == 422


async def test_inline_pinned_members_freeze_content_without_inventing_library_versions(config):
    from agent_catalog.catalog import prepare_lineup, restore_lineup
    from agent_catalog.schemas import MemberSpec
    from pydantic import ValidationError

    _, actor = await new_root()
    team = TeamSpec(name="Fixed temporary roster", preset_members=[{
        "alias": "review", "inline": spec(), "version_policy": "pinned",
    }], policy={"member_creation": "run_scoped", "member_selection": "explicit_only"})
    prepared = await prepare_lineup(team, actor, skills=[])
    saved = prepared.saved()
    team.preset_members[0].inline.instruction = "Later edit"
    restored = restore_lineup(saved)
    assert restored.members[0][1].spec.instruction == spec().instruction
    assert restored.members[0][2] is None
    assert restored.grant["member_selection"] == "explicit_only"
    with pytest.raises(ValidationError, match="published version_id"):
        MemberSpec(alias="review", agent_ref="builtin:reviewer", version_policy="pinned")
    with pytest.raises(ValidationError, match="no library version_id"):
        MemberSpec(alias="review", inline=spec(), version_id="invented")


async def test_inline_policy_conflict_is_actionable_and_never_silently_widens_grant(config):
    from agent_catalog.catalog import prepare_lineup

    _, actor = await new_root()
    team = TeamSpec(name="Invalid temporary roster", preset_members=[{"alias": "review", "inline": spec()}],
        policy={"member_creation": "disabled", "member_selection": "explicit_only"})
    with pytest.raises(TeamError) as error:
        await prepare_lineup(team, actor, skills=[])
    assert error.value.code == "INVALID_TEAM_POLICY"
    assert "run_scoped" in str(error.value)
    assert team.policy.member_creation == "disabled"
    config.team_generated_members_enabled = False
    team.policy.member_creation = "run_scoped"
    with pytest.raises(TeamError) as error:
        await prepare_lineup(team, actor, skills=[])
    assert error.value.code == "AUTHORITY_REVOKED"
    assert "deployment" in str(error.value)


async def test_definition_tenant_fences_revision_and_idempotency_conflicts(config):
    _, actor = await new_root()
    created = await repository.create("agent", actor, "create", spec())
    changed = spec(default_model="openai/test")
    with pytest.raises(TeamError) as error:
        await repository.create("agent", actor, "create", changed)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(TeamError) as error:
        await repository.mutate("agent", created["id"], actor, "stale", "archive", 99)
    assert error.value.code == "STALE_REVISION"
    for foreign in (replace(actor, owner_user_id="foreign"), replace(actor, workspace_id="foreign")):
        with pytest.raises(TeamError) as error:
            await repository.get("agent", created["id"], foreign)
        assert error.value.status == 404
    archived = await repository.mutate("agent", created["id"], actor, "archive", "archive", 1)
    assert archived["status"] == "archived"
    with pytest.raises(TeamError):
        await repository.get("agent", created["id"], actor, active_only=True)
    copy = await repository.create("agent", actor, "new", spec())
    assert copy["id"] != created["id"]


async def test_lineup_confirmation_creates_frozen_independent_sessions_in_one_transaction(config):
    from agent_catalog.catalog import prepare_lineup
    from db.models.question import QuestionCheckpoint
    from question.continuation import _apply
    root_id, actor = await new_root()
    team = TeamSpec(name="Evidence team", preset_members=[{"alias": "research", "agent_ref": "builtin:researcher"}, {"alias": "review", "agent_ref": "builtin:reviewer"}])
    lineup = await prepare_lineup(team, actor)
    checkpoint = QuestionCheckpoint(id="q-" + root_id, session_id=root_id, user_id=actor.owner_user_id, generation=1,
        status="answered", answers=[["开始"]], questions=[{"question": "Start?"}],
        continuation={"kind": "team_lineup", "title": "Test run", "goal": "Compare evidence", "lineup": lineup.saved()})
    async with get_db_session() as db:
        root = await db.get(Session, root_id)
        result, events = await _apply(db, root, checkpoint)
        assert root.agent == "team"
    run_id = result["metadata"]["team_run_id"]
    state = await snapshot(run_id, actor, rebuild=True)
    assert len(state["members"]) == 3 and state["run"]["state"] == "running"
    async with get_db_session() as db:
        children = (await db.execute(select(Session).where(Session.parent_id == root_id))).scalars().all()
        admissions = (await db.execute(select(TeamEvent).where(TeamEvent.team_run_id == run_id, TeamEvent.kind == "team.member.admitted"))).scalars().all()
    assert len(children) == 2 and len({row.id for row in children}) == 2
    assert all(row.kind == "team_member" and row.workspace_id == actor.workspace_id and row.model == "openai/test" for row in children)
    assert all("authority" in row.payload["data"] for row in admissions)
    assert {event["type"] for event in events} == {"session.updated", "team.run.updated"}


async def test_rejecting_lineup_never_creates_a_run(config):
    from db.models.question import QuestionCheckpoint
    from question.continuation import _apply
    root_id, actor = await new_root()
    checkpoint = QuestionCheckpoint(id="q-" + root_id, session_id=root_id, user_id=actor.owner_user_id,
        status="rejected", questions=[{"question": "Start?"}], continuation={"kind": "team_lineup"})
    async with get_db_session() as db:
        root = await db.get(Session, root_id)
        root.agent = "team"
        result, _ = await _apply(db, root, checkpoint)
        assert root.agent == "build" and result["metadata"]["rejected"]
        assert not (await db.execute(select(TeamRun.id).where(TeamRun.root_session_id == root_id))).first()


async def test_library_http_contract_and_tenant_isolation(config, monkeypatch):
    from api import agent_teams
    from auth.middleware import get_current_user
    from auth.workspace import get_workspace
    root_id, actor = await new_root()
    monkeypatch.setattr(agent_teams, "get_config", lambda: config)
    app = FastAPI()
    app.include_router(agent_teams.router)
    user = {"user_id": actor.owner_user_id, "workspace_id": actor.workspace_id}
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_workspace] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.post("/api/agent-definitions", json={"spec": spec().model_dump()})
        assert missing.status_code == 422
        created = await client.post("/api/agent-definitions", headers={"Idempotency-Key": "create"}, json={"spec": spec().model_dump()})
        assert created.status_code == 201, created.text
        definition_id = created.json()["id"]
        published = await client.post(f"/api/agent-definitions/{definition_id}/versions", headers={"Idempotency-Key": "publish"}, json={"expected_revision": 1})
        assert published.status_code == 200, published.text
        user["workspace_id"] = "another-workspace"
        forbidden = await client.get(f"/api/agent-definitions/{definition_id}")
        assert forbidden.status_code == 404
        assert forbidden.json()["detail"]["code"] == "AGENT_NOT_ACCESSIBLE"
        # There is intentionally no direct-start endpoint that could bypass
        # the existing durable question confirmation.
        assert (await client.post("/api/team-runs", json={})).status_code == 405
