"""Two-process team journal checks against an explicitly selected test database.

Run after Alembic upgrade, with TEAM_TEST_DATABASE_URL set. This creates only
uniquely named test-owned rows and never contacts a model or a tool provider.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, update, text
from sqlalchemy.exc import IntegrityError

import db.models  # noqa: F401
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_inbox import AgentInboxItem
from db.models.project import Project
from db.models.session import Session
from db.models.team import TeamEvent, TeamRun
from db.models.user import User
from db.models.workspace import Workspace
from agent_catalog.schemas import TeamPolicy
from team import commands
from team.errors import TeamError
from team.journal import Actor, Writer, command, digest, snapshot, utcnow, write_transaction
from team.state import empty_state


async def seed(*, scope=None, desktop=False):
    suffix = uuid.uuid4().hex[:14]
    user_id, workspace_id, project_id, root_id, member_id, run_id = [f"{prefix}_{suffix}" for prefix in ("teamtestuser", "teamtestws", "teamtestproj", "teamtestroot", "teamtestmember", "teamtest")]
    now = utcnow()
    async with get_db_session() as db:
        if scope:
            user_id, workspace_id = scope.owner_user_id, scope.workspace_id
        else:
            user = User(id=user_id, username=user_id, created_at=now, updated_at=now)
            db.add(user)
            await db.flush()
            db.add(Workspace(id=workspace_id, owner_user_id=user_id, name="Team journal verification", created_at=now, updated_at=now))
            await db.flush()
            user.default_workspace_id = workspace_id
        db.add(Project(id=project_id, user_id=user_id, workspace_id=workspace_id, name="Verification", slug=project_id, created_at=now, updated_at=now))
        await db.flush()
        db.add(Session(id=root_id, user_id=user_id, workspace_id=workspace_id, project_id=project_id, agent="team", kind="normal", model="test/model", created_at=now, updated_at=now))
        await db.flush()
        db.add(Session(id=member_id, user_id=user_id, workspace_id=workspace_id, project_id=project_id, parent_id=root_id,
            agent="team_member", kind="team_member", model="test/model", created_at=now, updated_at=now))
    actor = Actor(user_id, workspace_id, "server")
    policy = TeamPolicy().model_dump(mode="json")
    grant = {"version": 1, "budget_credits": "10", "paid_tools": {"image_gen": {"per_call": "6", "total": "10"}}}
    async with write_transaction() as db:
        run = TeamRun(id=run_id, root_session_id=root_id, owner_user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, title="PostgreSQL verification", goal="Verify serialization", state="provisioning",
            policy_snapshot=policy, grant_snapshot=grant, revision=1, session_active=1, project_active=1, created_at=now, updated_at=now)
        db.add(run)
        await db.flush()
        writer = Writer(db, run, actor, digest("seed"), digest({}), empty_state(run_id))
        writer.append("team.run.created", "run", {"id": run_id, "root_session_id": root_id, "owner_user_id": user_id,
            "workspace_id": workspace_id, "project_id": project_id, "state": "provisioning", "revision": 1, "created_at": now.isoformat(),
            "policy_snapshot": policy, "grant_snapshot": grant})
        for mid, role in ((root_id, "coordinator"), (member_id, "member")):
            writer.append("team.member.admitted", "member", {"id": mid, "role": role, "alias": role, "source": "builtin"})
            commands.member_status(writer, mid, membership_state="active")
        commands.run_status(writer, "running")
        await commands.create_task(writer, {"title": "Check evidence", "description": "Compare test inputs", "owner_member_id": member_id,
            "expected_output": "Findings", "acceptance_criteria": "Accurate", "exclusive_group": "desktop" if desktop else None})
        writer.finish({"seeded": True})
    return run_id, actor, member_id


async def worker(args):
    actor = Actor(args.user, args.workspace, "server")
    if args.operation == "dispatch":
        mutate = commands.dispatch_ready
    else:
        async def mutate(writer):
            writer.append("team.budget.reserved", "reservation", {"id": writer.key, "tool": "image_gen", "amount": "6"})
            return {"reserved": True}
    try:
        result = await command(args.run, actor, args.key, {"operation": args.operation}, mutate)
        print(json.dumps({"ok": True, "result": result}))
    except TeamError as exc:
        print(json.dumps({"ok": False, "code": exc.code}))


async def race(run_id, actor, operation, *, second_run=None):
    async def launch(index):
        process = await asyncio.create_subprocess_exec(sys.executable, __file__, "--operation", operation,
            "--run", second_run if second_run and index == 2 else run_id, "--user", actor.owner_user_id, "--workspace", actor.workspace_id,
            "--key", f"{operation}:{index}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(stderr.decode()[-5000:])
        return json.loads(stdout.decode().splitlines()[-1])
    return await asyncio.gather(launch(1), launch(2))


async def verify():
    run_id, actor, member_id = await seed()
    dispatch = await race(run_id, actor, "dispatch")
    assert all(item["ok"] for item in dispatch), dispatch
    assert sum(len(item["result"]["dispatched"]) for item in dispatch) == 1, dispatch
    reservations = await race(run_id, actor, "reserve")
    assert all(item["ok"] for item in reservations), reservations
    state = await snapshot(run_id, actor)
    assert state == await snapshot(run_id, actor, rebuild=True)
    async with get_db_session() as db:
        events = (await db.execute(select(TeamEvent).where(TeamEvent.team_run_id == run_id).order_by(TeamEvent.sequence))).scalars().all()
        inbox = (await db.execute(select(AgentInboxItem).where(AgentInboxItem.session_id == member_id))).scalars().all()
        head = await db.scalar(text("SELECT version_num FROM alembic_version"))
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert len(inbox) == 1 and inbox[0].source_type == "team_task"
    try:
        async with get_db_session() as db:
            await db.execute(update(TeamRun).where(TeamRun.id == run_id).values(session_active=None))
    except IntegrityError:
        pass
    else:
        raise AssertionError("Nonterminal NULL active flag bypassed database constraint")
    for outsider in (replace(actor, workspace_id="other"), replace(actor, owner_user_id="other")):
        try:
            await snapshot(run_id, outsider)
        except TeamError as exc:
            assert exc.status == 404
        else:
            raise AssertionError("Tenant isolation failed")
    desktop_a, _, member_a = await seed(scope=actor, desktop=True)
    desktop_b, _, member_b = await seed(scope=actor, desktop=True)
    desktops = await race(desktop_a, actor, "dispatch", second_run=desktop_b)
    assert all(item["ok"] for item in desktops), desktops
    assert sum(len(item["result"]["dispatched"]) for item in desktops) == 1, desktops
    winner = 0 if desktops[0]["result"]["dispatched"] else 1
    winning_run, winning_member = [(desktop_a, member_a), (desktop_b, member_b)][winner]
    losing_run = [desktop_a, desktop_b][1 - winner]
    async def wait(writer):
        commands.member_status(writer, winning_member, execution_state="waiting")
        return {"waiting": True}
    await command(winning_run, actor, "wait", {}, wait)
    assert not (await command(losing_run, actor, "waiting-lock", {}, commands.dispatch_ready))["dispatched"]
    await verify_usage_fences()
    print(json.dumps({"database": "PostgreSQL", "migration": head, "run_id": run_id,
        "two_process_dispatch": "passed", "two_process_workspace_desktop": "passed", "two_process_effect_reservations": "passed", "cache_replay": "passed",
        "event_sequences": "passed", "inbox_deduplication": "passed", "active_null_constraint": "passed", "tenant_isolation": "passed",
        "pending_usage_fences": "passed", "unknown_cost_attention": "passed"}, indent=2))


async def verify_usage_fences():
    from db.models.agent_driver import AgentDriverState
    from db.models.billing import UsageEvent
    from team.budget import model_usage
    from team.history import list_runs
    run_id, actor, member_id = await seed()
    now = utcnow()
    usage_id = "teamtestusage_" + uuid.uuid4().hex
    async with get_db_session() as db:
        db.add(AgentDriverState(session_id=member_id, user_id=actor.owner_user_id, generation=1,
            run_id="live-request", phase="running", started_at=now, updated_at=now,
            lease_expires_at=now + timedelta(minutes=5)))
        db.add(UsageEvent(id=usage_id, idempotency_key=usage_id, workspace_id=actor.workspace_id,
            user_id=actor.owner_user_id, session_id=member_id, session_title="PG fenced usage",
            model_id="test/model", kind="chat", tokens={}, total_tokens=0, credits=None,
            status="pending", pricing={"request_fence": {"run_id": "live-request", "generation": 1}}, created_at=now))
    async with get_db_session() as db:
        assert await model_usage(db, await db.get(TeamRun, run_id), [member_id]) == (Decimal(0), 0)
    assert not (await list_runs(actor))["items"][0]["summary"]["needs_attention"]
    async with get_db_session() as db:
        (await db.get(AgentDriverState, member_id)).generation = 2
    async with get_db_session() as db:
        assert await model_usage(db, await db.get(TeamRun, run_id), [member_id]) == (Decimal(0), 1)
        meter = await db.get(UsageEvent, usage_id)
        assert meter.status == "pending" and meter.credits is None
    assert (await list_runs(actor))["items"][0]["summary"]["needs_attention"]


async def main():
    parser = argparse.ArgumentParser()
    for key in ("operation", "run", "user", "workspace", "key"):
        parser.add_argument("--" + key)
    args = parser.parse_args()
    url = os.environ.get("TEAM_TEST_DATABASE_URL", "")
    if not url.startswith("postgresql+asyncpg://"):
        raise SystemExit("Set TEAM_TEST_DATABASE_URL to an isolated PostgreSQL test database")
    init_engine(url)
    try:
        await worker(args) if args.operation else await verify()
    finally:
        await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
