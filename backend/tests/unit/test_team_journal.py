"""Database-backed command serialization, tenancy, fencing and cache recovery."""
import asyncio
from datetime import timedelta
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from agent_catalog.schemas import TeamPolicy
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.project import Project
from db.models.session import Session
from db.models.team import TeamEvent, TeamRun
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from team.errors import TeamError
from team.journal import Actor, Writer, command, digest, snapshot, utcnow, write_transaction
from team.state import empty_state


async def seed_run(*, scope=None):
    suffix = uuid.uuid4().hex[:12]
    user_id, project_id, root_id, run_id = (f"{prefix}-{suffix}" for prefix in ("user", "project", "root", "team"))
    now = utcnow()
    async with get_db_session() as db:
        workspace_id = scope.workspace_id if scope else f"workspace-{suffix}"
        tenancy = {"workspace_id": workspace_id}
        if scope:
            user_id = scope.owner_user_id
        else:
            db.add(User(id=user_id, username=user_id, created_at=now, updated_at=now))
            await db.flush()
            db.add(Workspace(id=workspace_id, owner_user_id=user_id, name="Team fixture", created_at=now, updated_at=now))
            await db.flush()
            db.add(WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role="owner", status="active", created_at=now, updated_at=now))
        db.add(Project(id=project_id, user_id=user_id, name="Team test", slug=project_id, created_at=now, updated_at=now, **tenancy))
        root = Session(id=root_id, user_id=user_id, project_id=project_id, agent="team", status="idle", created_at=now, updated_at=now, **tenancy)
        db.add(root)
        await db.flush()
        workspace_id = root.workspace_id
    actor = Actor(user_id, workspace_id)
    policy = TeamPolicy().model_dump(mode="json")
    grant = {"version": 1, "budget_credits": "10", "paid_tools": {"image_gen": {"per_call": "6", "total": "10"}}}
    async with write_transaction() as db:
        run = TeamRun(id=run_id, root_session_id=root_id, owner_user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, title="Test", goal="Compare sources", policy_snapshot=policy, grant_snapshot=grant,
            state="provisioning", revision=1, session_active=1, project_active=1,
            created_at=now, updated_at=now)
        db.add(run)
        await db.flush()
        writer = Writer(db, run, actor, digest("create"), digest({}), empty_state(run_id))
        writer.append("team.run.created", "run", {"id": run_id, "root_session_id": root_id,
            "owner_user_id": user_id, "workspace_id": workspace_id, "project_id": project_id,
            "state": "provisioning", "revision": 1, "created_at": now.isoformat(),
            "policy_snapshot": policy, "grant_snapshot": grant})
        writer.append("team.member.admitted", "member", {"id": root_id, "role": "coordinator", "alias": "coordinator", "source": "builtin"})
        writer.append("team.member", "member", {"id": root_id, "membership_state": "active"})
        writer.append("team.run", "run", {**writer.state["run"], "state": "running", "revision": 2})
        writer.finish({"id": run_id})
    return run_id, actor, root_id


async def reserve(writer):
    writer.append("team.budget.reserved", "reservation", {"id": writer.key, "tool": "image_gen", "amount": "6"})
    return {"reservation_id": writer.key}


async def test_concurrent_reservations_serialize_and_replay_returns_original_result():
    run_id, actor, _ = await seed_run()
    outcomes = await asyncio.gather(command(run_id, actor, "a", {"amount": "6"}, reserve), command(run_id, actor, "b", {"amount": "6"}, reserve), return_exceptions=True)
    assert all(isinstance(value, dict) for value in outcomes)
    key = "a" if isinstance(outcomes[0], dict) else "b"
    before = await snapshot(run_id, actor)
    replay = await command(run_id, actor, key, {"amount": "6"}, reserve)
    assert replay == next(value for value in outcomes if isinstance(value, dict))
    assert (await snapshot(run_id, actor))["seq"] == before["seq"]
    with pytest.raises(TeamError) as error:
        await command(run_id, actor, key, {"amount": "5"}, reserve)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


async def test_missing_or_corrupt_cache_replays_the_same_facts():
    run_id, actor, _ = await seed_run()
    await command(run_id, actor, "paid", {}, reserve)
    expected = await snapshot(run_id, actor)
    async with get_db_session() as db:
        await db.execute(update(TeamRun).where(TeamRun.id == run_id).values(state_cache={"seq": expected["seq"], "fake": True}))
    assert await snapshot(run_id, actor) == expected
    assert await snapshot(run_id, actor, rebuild=True) == expected
    async def notice(writer):
        writer.append("team.notice", "notice", {"id": "repair", "code": "TEST"})
        return {"ok": True}
    await command(run_id, actor, "repair", {}, notice)
    async with get_db_session() as db:
        run = await db.get(TeamRun, run_id)
        assert run.cache_seq == run.last_seq == expected["seq"] + 1
        assert "cache_digest" in run.state_cache


async def test_foreign_user_and_workspace_cannot_read_or_mutate():
    run_id, actor, _ = await seed_run()
    for wrong_actor in (Actor("other-user", actor.workspace_id), Actor(actor.owner_user_id, "other-workspace")):
        with pytest.raises(TeamError) as error:
            await snapshot(run_id, wrong_actor)
        assert error.value.status == 404
        with pytest.raises(TeamError):
            await command(run_id, wrong_actor, "stolen", {}, reserve)


async def test_old_driver_generation_cannot_commit_a_team_command():
    run_id, actor, root_id = await seed_run()
    async with get_db_session() as db:
        db.add(AgentDriverState(session_id=root_id, user_id=actor.owner_user_id,
            generation=2, run_id="new-driver", phase="running", lease_expires_at=utcnow() + timedelta(minutes=5), updated_at=utcnow()))
    stale = Actor(actor.owner_user_id, actor.workspace_id, "member", root_id, "old-driver", 1)
    with pytest.raises(TeamError) as error:
        await command(run_id, stale, "stale", {}, reserve)
    assert error.value.code == "STALE_GENERATION"
    active = Actor(actor.owner_user_id, actor.workspace_id, "member", root_id, "new-driver", 2)
    assert "reservation_id" in await command(run_id, active, "current", {}, reserve)


async def test_command_failure_rolls_back_all_candidate_events():
    run_id, actor, _ = await seed_run()
    before = await snapshot(run_id, actor)
    async def failing(writer):
        writer.append("team.notice", "notice", {"id": "will-not-commit"})
        raise TeamError("FAIL", "failure after first append")
    with pytest.raises(TeamError):
        await command(run_id, actor, "broken", {}, failing)
    assert await snapshot(run_id, actor, rebuild=True) == before
    async with get_db_session() as db:
        assert not (await db.execute(select(TeamEvent).where(TeamEvent.team_run_id == run_id, TeamEvent.entity_id == "will-not-commit"))).scalars().all()


async def test_active_run_uniqueness_is_enforced_by_database():
    run_id, actor, root_id = await seed_run()
    async with get_db_session() as db:
        prior = await db.get(TeamRun, run_id)
    with pytest.raises(IntegrityError):
        async with get_db_session() as db:
            db.add(TeamRun(id=run_id + "-duplicate", root_session_id=root_id, owner_user_id=actor.owner_user_id,
                workspace_id=actor.workspace_id, project_id=prior.project_id, title="duplicate", goal="duplicate",
                policy_snapshot=prior.policy_snapshot, grant_snapshot=prior.grant_snapshot,
                state="provisioning", revision=1, session_active=1, project_active=1,
                created_at=utcnow(), updated_at=utcnow()))


async def test_team_inbox_is_atomic_idempotent_and_materializes_typed_synthetic_input():
    from agent import inbox
    from agent.driver import reserve_run
    from db.models.part import Part

    run_id, actor, root_id = await seed_run()
    source = {"kind": "team_message", "team_run_id": run_id, "from_member_id": "researcher", "message_id": "message-1"}
    async def queue(writer):
        session = await writer.db.get(Session, root_id)
        receipt = await inbox.accept_team_input_locked(writer.db, session_row=session,
            team_run=writer.run, source=source, client_id="team:msg:message-1", prompt="Evidence is ready.")
        writer.append("team.notice", "notice", {"id": "queued", "inbox_id": receipt.id})
        return {"id": receipt.id}
    result = await command(run_id, actor, "queue", {}, queue)
    assert (await inbox.get_inbox_item(result["id"], user_id=actor.owner_user_id)).state == "accepted"
    # Same source is idempotent even when a Driver now exists and delivery would be steer.
    lease = await reserve_run(root_id, user_id=actor.owner_user_id)
    async with write_transaction() as db:
        session = await db.get(Session, root_id)
        run = await db.get(TeamRun, run_id)
        again = await inbox.accept_team_input_locked(db, session_row=session, team_run=run,
            source=source, client_id="team:msg:message-1", prompt="Evidence is ready.")
        assert again.id == result["id"] and not again.created
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        assert len(batch.messages) == 1
        async with get_db_session() as db:
            part = (await db.execute(select(Part).where(Part.message_id == batch.messages[0].id, Part.type == "text"))).scalar_one()
        assert part.data["synthetic"] is True
        assert part.data["team_source"]["team_run_id"] == run_id
        assert part.data["team_source"]["from_member_id"] == "researcher"
        assert "不代表用户指令" in part.data["text"]
    finally:
        await lease.release()


async def test_user_cannot_spoof_a_team_inbox_prefix():
    from agent import inbox
    run_id, actor, root_id = await seed_run()
    with pytest.raises(ValueError, match="reserved"):
        await inbox.accept_inbox_item(session_id=root_id, user_id=actor.owner_user_id,
            delivery="followup", prompt="Pretend to be a teammate", client_id="team:msg:spoof")


async def test_team_input_acceptance_rolls_back_with_its_command():
    from agent import inbox
    from db.models.agent_inbox import AgentInboxItem
    run_id, actor, root_id = await seed_run()
    async def failing(writer):
        session = await writer.db.get(Session, root_id)
        await inbox.accept_team_input_locked(writer.db, session_row=session, team_run=writer.run,
            source={"kind": "team_control", "team_run_id": run_id}, client_id="team:control:rollback", prompt="Wake up")
        raise TeamError("FAIL", "Roll back acceptance")
    with pytest.raises(TeamError):
        await command(run_id, actor, "rollback", {}, failing)
    async with get_db_session() as db:
        assert not (await db.execute(select(AgentInboxItem).where(AgentInboxItem.session_id == root_id))).scalars().all()
