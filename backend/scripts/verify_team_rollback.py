"""Rehearse compatibility rollback gates in an isolated PostgreSQL database.

Retains every fixture and journal. A child holds a live Driver while its parent
pauses it; another child exits after an external-send fixture. No model/media
provider is called. The committed pre-team authority loader is exercised, not
an entire old application deployment; the report states that distinction.
"""
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from agent import effect_ledger
from agent_catalog.compiler import compile_agent
from agent_catalog.schemas import AgentSpec, TeamPolicy
from core import config as config_module
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_inbox import AgentInboxItem
from db.models.external_effect import ExternalEffect
from db.models.session import Session
from db.models.team import TeamEvent, TeamRun
from scripts.verify_team_postgres import seed
from scripts.verify_team_recovery import configure, crash, recover_claim, verify_remote_pause
from team import paid_tools, scheduler
from team.errors import TeamError
from team.journal import Actor, snapshot, utcnow
from team.service import control, start_confirmed_locked


async def retained(run_id):
    async with get_db_session() as db:
        run = await db.get(TeamRun, run_id)
        actor = Actor(run.owner_user_id, run.workspace_id)
        state = await snapshot(run_id, actor)
        sessions = list((await db.scalars(select(Session.id).where(Session.id.in_(state["members"]), Session.is_deleted.is_(False)))).all())
        events = await db.scalar(select(func.count(TeamEvent.id)).where(TeamEvent.team_run_id == run_id))
        inbox = await db.scalar(select(func.count(AgentInboxItem.id)).where(AgentInboxItem.session_id.in_(sessions)))
        return {"run_id": run_id, "actor": actor, "state": state, "counts": {"runs": 1, "sessions": len(sessions), "events": events, "inbox": inbox}}


async def verify():
    config = config_module.get_config()
    # This uses actual current Driver/Inbox code in separate processes.
    pause = await verify_remote_pause()
    known = await retained(pause["run_id"])
    actor, run = known["actor"], known["run_id"]
    for field in ("team_admission_enabled", "team_generated_members_enabled", "team_tools_enabled", "team_ui_enabled"):
        setattr(config, field, False)
    coordinator = compile_agent(AgentSpec(name="Rollback fixture", description="No provider calls",
        when_to_use="Rollback verification", instruction="Do not run a provider.",
        default_model="openai/test", tool_allowlist=[]), config=config, role="coordinator")
    async with get_db_session() as db:
        root = await db.get(Session, known["state"]["run"]["root_session_id"])
        try:
            await start_confirmed_locked(db, root=root, question_id="rollback-gate", title="Rejected admission",
                goal="Must not start", policy=TeamPolicy(), grant={}, coordinator=coordinator, members=[])
        except TeamError as exc:
            assert exc.code == "TEAM_ADMISSION_DISABLED"
        else:
            raise AssertionError("Admission gate did not reject a new confirmation")
    assert (await retained(run))["counts"] == known["counts"]
    await control(run, actor, "rollback-cancel", "cancel", known["state"]["run"]["revision"])
    await scheduler.tick(run, actor)
    closed = await retained(run)
    assert closed["state"]["run"]["state"] == "canceled"
    assert not any(attempt["state"] == "running" for attempt in closed["state"]["attempts"].values())
    from team.runtime import runtime
    async with get_db_session() as db:
        assert not any(row.live for row in (await runtime.observe(db, closed["state"]["members"], actor.owner_user_id)).values())
    for name in ("runs", "sessions", "inbox"):
        assert closed["counts"][name] == known["counts"][name]
    assert closed["counts"]["events"] >= known["counts"]["events"]

    # Keep unknown work on the compatible side; never force-release its money
    # or run it through the legacy authority path.
    unknown_run, unknown_actor, member = await seed()
    await crash("external_receipt_lost", unknown_run)
    await recover_claim(unknown_run, unknown_actor, member)
    await scheduler.tick(unknown_run, unknown_actor)
    ambiguous = await snapshot(unknown_run, unknown_actor)
    reservation = next(iter(ambiguous["reservations"].values()))
    async with get_db_session() as db:
        effect = await db.get(ExternalEffect, reservation["external_id"])
        from datetime import timedelta
        effect.claim_expires_at = utcnow() - timedelta(seconds=1)
    assert await effect_ledger.recover_effect_once(reservation["external_id"]) == "manual_review"
    await paid_tools.reconcile(unknown_run, unknown_actor)
    unknown_owner = replace(unknown_actor, kind="user")
    ambiguous = await snapshot(unknown_run, unknown_owner)
    await control(unknown_run, unknown_owner, "isolate-unknown", "pause", ambiguous["run"]["revision"], "rollback_external_review")
    await scheduler.tick(unknown_run, unknown_owner)
    isolated = await retained(unknown_run)
    assert isolated["state"]["run"]["state"] == "paused"
    assert isolated["state"]["reservations"][reservation["id"]]["state"] == "reserved"
    assert not any(row["state"] == "running" for row in isolated["state"]["attempts"].values())
    assert not await scheduler.can_wake(member, unknown_owner.owner_user_id)

    # Read the exact committed pre-team implementation. This cannot start an
    # Agent or network request; only its authority loader is called.
    repository = Path(__file__).resolve().parents[2]
    source = subprocess.check_output(["git", "show", "HEAD:backend/agent/subagent_authority.py"], cwd=repository)
    if b"from team.runtime_binding import load_binding" in source:
        raise AssertionError("HEAD is no longer a pre-team baseline; select an explicit older release for this rehearsal")
    name = "_team_rollback_legacy_authority"
    legacy = types.ModuleType(name)
    sys.modules[name] = legacy
    exec(compile(source, "committed:agent/subagent_authority.py", "exec"), legacy.__dict__)
    async with get_db_session() as db:
        root = await db.get(Session, closed["state"]["run"]["root_session_id"])
        worker = await db.get(Session, next(mid for mid, row in closed["state"]["members"].items() if row["role"] == "member"))
        migration = await db.scalar(text("SELECT version_num FROM alembic_version"))
    assert root.agent == "build" and await legacy.load_subagent_authority(root) is None
    try:
        await legacy.load_subagent_authority(worker)
    except legacy.SubagentAuthorityError:
        pass
    else:
        raise AssertionError("Legacy authority loader accepted a team member as ordinary work")
    assert (await retained(run))["counts"] == closed["counts"]
    assert (await retained(unknown_run))["counts"] == isolated["counts"]
    return {"status": "passed", "database": "PostgreSQL", "migration_retained": migration,
        "scope": "Compatibility gates and committed legacy authority loader; not a full old-binary deployment or real provider test",
        "gates": "all disabled", "known_run": {"id": run, "state": "canceled", "before": known["counts"], "after": closed["counts"]},
        "isolated_unknown": {"id": unknown_run, "state": "paused", "reservation": reservation["id"], "reserved_credits": reservation["amount"],
            "effect_id": reservation["external_id"], "effect_state": "manual_review", "retained": isolated["counts"]},
        "old_authority_loader_sha256": hashlib.sha256(source).hexdigest(), "old_member_refused": True,
        "root_fallback": "build", "running_attempts": 0, "destructive_downgrade": False}


async def main():
    url = make_url(os.environ.get("TEAM_TEST_DATABASE_URL", "sqlite://"))
    if url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost"} or not any(word in (url.database or "") for word in ("test", "check")):
        raise SystemExit("An explicit loopback PostgreSQL test/check database is required")
    configure()
    init_engine(url.render_as_string(hide_password=False))
    try:
        print(json.dumps(await verify(), indent=2))
    finally:
        await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
