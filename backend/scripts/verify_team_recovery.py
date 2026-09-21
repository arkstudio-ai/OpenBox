"""Crash real child processes at team commit boundaries, without providers.

Run against a migrated, loopback PostgreSQL database whose name contains
``test`` or ``check``. TEAM_TEST_DATABASE_URL is required. Every scenario owns
unique rows, retains its evidence, and uses a recording wake adapter instead
of starting models. ``os._exit`` deliberately bypasses transaction cleanup.
Lease timestamps are advanced only for these fixtures to avoid a minute-long
sleep. The printed report distinguishes these storage tests from live models.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from agent import driver, effect_ledger, inbox
from agent_catalog.compiler import compile_agent
from agent_catalog.schemas import AgentSpec, MemberSpec
from core import config as config_module
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_inbox import AgentInboxItem
from db.models.external_effect import ExternalEffect
from db.models.message import Message
from db.models.session import Session
from db.models.team import TeamEvent, TeamRun
from scripts.verify_team_postgres import seed
from team import capacity, commands, lifecycle, paid_tools, runtime, runtime_binding, scheduler
from team.errors import TeamError
from team.journal import Actor, command, snapshot, utcnow
from team.service import admit_member

CRASH_EXIT = 73
_wakes: list[str] = []


def configure() -> None:
    # Explicit in-memory configuration; never read deployment credentials.
    config = config_module.OpenBoxConfig(model="openai/test", models=[{"id": "openai/test"}],
        provider={"openai": {"api_key": "fixture-only", "base_url": "http://127.0.0.1:9",
            "subagent_capabilities": ["model", "tool_filter", "persona", "output_schema"]}},
        team_admission_enabled=True, team_generated_members_enabled=True,
        team_wake_debounce_seconds=0, team_max_running_members=3,
        team_reserved_agent_slots=2, max_concurrent_agents=5)
    config_module.get_config = lambda: config
    runtime_binding.get_config = lambda: config

    async def record_wake(member_id, _user_id):
        _wakes.append(member_id)
    runtime.runtime.wake = record_wake


async def state_for(run_id: str):
    async with get_db_session() as db:
        run = await db.get(TeamRun, run_id)
        assert run is not None and run.id.startswith("teamtest_")
        actor = Actor(run.owner_user_id, run.workspace_id, "server")
    state = await snapshot(run_id, actor)
    member = next(mid for mid, row in state["members"].items() if row["role"] == "member")
    return actor, state, member


def bind(run_id, actor, state, member, role="member"):
    return runtime_binding._current.set(runtime_binding.RuntimeBinding(run_id, member,
        actor.owner_user_id, actor.workspace_id, state["run"]["project_id"], role, {}, {}))


async def admit(writer):
    spec = AgentSpec(name="Recovery fixture", description="Independent recovery member",
        when_to_use="Only in the isolated recovery verifier", instruction="Do not call a provider.",
        default_model="openai/test", tool_allowlist=[])
    return await admit_member(writer, MemberSpec(alias="recovered", inline=spec),
        compile_agent(spec, config=config_module.get_config()), source="coordinator")


async def dispatch(run, actor):
    return await command(run, actor, "dispatch", {}, commands.dispatch_ready)


async def mail(run, actor, member):
    return await command(run, actor, "mail", {}, lambda writer: commands.queue_message(writer,
        to_member_id=member, body="A durable peer message"))


async def result(run, actor):
    async def submit(writer):
        task = next(iter(writer.state["tasks"].values()))
        return await commands.update_task(writer, task["id"], task["revision"], "submit", summary="Checked fixture result")
    return await command(run, actor, "submit", {}, submit)


async def child(operation, run):
    actor, state, member = await state_for(run)
    if operation == "active_execution":
        await dispatch(run, actor)
        token = bind(run, actor, state, member)
        lease = await driver.reserve_run(member, actor.owner_user_id)
        await lifecycle.turn_started(lease)
        await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        print(json.dumps({"ready": True, "member": member, "generation": lease.generation}), flush=True)
        try:
            async with asyncio.timeout(20):
                while not await lease.abort_was_requested():
                    await asyncio.sleep(0.1)
            await inbox.settle_claimed_inbox_items(lease, result_message_id=None, outcome="aborted")
        finally:
            await lease.release()
            runtime_binding._current.reset(token)
        return
    if operation == "driver_capacity":
        config_module.get_config().team_max_running_members = 1
        await dispatch(run, actor)
        _, other_actor, occupant = await seed()
        await driver.reserve_run(occupant, other_actor.owner_user_id)
        assert await inbox._reserve_and_claim(member, actor.owner_user_id) is None
        async def record(writer):
            writer.append("team.notice", "notice", {"id": "occupant-" + run, "code": "FIXTURE_QUOTA_OCCUPANT",
                "member_id": occupant})
            return {}
        await command(run, actor, "quota-occupant", {}, record)
    elif operation == "recovery_tick":
        await scheduler.tick(run, actor)
    elif operation == "owner_message":
        from team.owner_messages import OwnerMessage, send
        await send(run, replace(actor, kind="user"), "owner-correction", OwnerMessage(text="Keep this original user correction."))
    elif operation == "command_response_lost":
        await command(run, actor, "admit", {}, admit)
    elif operation == "provisioning_uncommitted":
        async def die_after_session(writer):
            await admit(writer)
            await writer.db.flush()
            os._exit(CRASH_EXIT)
        await command(run, actor, "admit", {}, die_after_session)
    elif operation == "queued_message":
        await mail(run, actor, member)
    elif operation == "inbox_before_receipt":
        original = runtime.runtime.enqueue
        async def die_after_enqueue(db, *args, **kwargs):
            await original(db, *args, **kwargs)
            await db.flush()
            os._exit(CRASH_EXIT)
        runtime.runtime.enqueue = die_after_enqueue
        await scheduler.tick(run, actor)
    elif operation == "dispatch_before_wake":
        await dispatch(run, actor)
    elif operation == "claimed_before_driver":
        await dispatch(run, actor)
        lease = await driver.reserve_run(member, actor.owner_user_id)
        await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    elif operation == "member_result_before_notify":
        await dispatch(run, actor)
        await result(run, actor)
    elif operation == "coordinator_wait":
        await dispatch(run, actor)
        root = state["run"]["root_session_id"]
        lease = await driver.reserve_run(root, actor.owner_user_id)
        token = bind(run, actor, state, root, "coordinator")
        member_actor = replace(actor, kind="member", member_id=root,
            driver_run_id=lease.run_id, generation=lease.generation)
        await command(run, member_actor, "wait", {},
            lambda writer: commands.wait_member(writer, writer.state["seq"]))
        await lease.release()
        runtime_binding._current.reset(token)
    elif operation == "natural_answer":
        await dispatch(run, actor)
        token = bind(run, actor, state, member)
        lease = await driver.reserve_run(member, actor.owner_user_id)
        await lifecycle.turn_started(lease)
        await lifecycle.turn_ended(lease, text="Checked fixture result", outcome="succeeded")
        await lease.release()
        runtime_binding._current.reset(token)
    elif operation in {"paid_before_send", "external_receipt_lost"}:
        await dispatch(run, actor)
        token = bind(run, actor, state, member)
        lease = await driver.reserve_run(member, actor.owner_user_id)
        await lifecycle.turn_started(lease)
        await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        fence = effect_ledger.EffectRunFence(member, actor.owner_user_id, lease.run_id, lease.generation)
        prepared = await effect_ledger.prepare_effect(fence, adapter="recovery-fixture", provider="recording-fixture",
            operation="create", logical_key="fixture", request_payload={"fixture": True},
            project_id=state["run"]["project_id"])
        effect_id = prepared.snapshot.effect_id
        # This fixture injects an admitted reservation through the journal.
        # Tool-price admission itself has separate adapter-level coverage.
        async def reserve(writer):
            attempt = next(iter(writer.state["attempts"].values()))
            writer.append("team.budget.reserved", "reservation", {"id": "fixture-" + run, "tool": "image_gen",
                "amount": "6", "member_id": member, "attempt_id": attempt["id"], "task_id": attempt["task_id"],
                "external_kind": "external_effect", "external_id": effect_id,
                "billing_keys": ["image:" + run], "expected_usage_count": 1})
            return {"reserved": True}
        await command(run, actor, "reserve", {}, reserve)
        if operation == "external_receipt_lost":
            claim = await effect_ledger.claim_effect_for_dispatch(effect_id, fence)
            await effect_ledger.mark_effect_submitting(claim)
            await effect_ledger.assert_effect_dispatchable(claim)
            # Durable recording provider: its journal call stands for the
            # externally observed send, separate from the effect receipt.
            async def observed(writer):
                writer.append("team.notice", "notice", {"id": "provider-" + run, "code": "FIXTURE_PROVIDER_CALLED"})
                return {"calls": 1}
            await command(run, actor, "provider-observed", {}, observed)
        runtime_binding._current.reset(token)
    elif operation == "corrupt_cache":
        async with get_db_session() as db:
            (await db.get(TeamRun, run)).state_cache = {"invalid_fixture": True}
    else:
        raise AssertionError(operation)
    os._exit(CRASH_EXIT)


async def crash(operation, run):
    process = await asyncio.create_subprocess_exec(sys.executable, __file__, "--child", operation, "--run", run,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output, error = await process.communicate()
    assert process.returncode == CRASH_EXIT, (operation, process.returncode, output.decode()[-1000:], error.decode()[-4000:])


async def receipts(member):
    async with get_db_session() as db:
        return list((await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == member))).all())


async def assert_consistent(run, actor):
    state = await snapshot(run, actor)
    assert state == await snapshot(run, actor, rebuild=True)
    async with get_db_session() as db:
        sequences = list((await db.scalars(select(TeamEvent.sequence).where(TeamEvent.team_run_id == run)
            .order_by(TeamEvent.sequence))).all())
    assert sequences == list(range(1, state["seq"] + 1))
    return state


async def expire_fixture(member):
    async with get_db_session() as db:
        row = await db.get(AgentDriverState, member)
        assert row and member.startswith("teamtestmember_")
        row.lease_expires_at = utcnow() - timedelta(seconds=1)


async def recover_claim(run, actor, member):
    before = (await receipts(member))[0]
    await expire_fixture(member)
    records = [row for row in await driver.recover_expired_driver_records() if row.session_id == member]
    assert len(records) == 1
    lease = await driver.reserve_recovered_run(records[0], initial_phase="reserved")
    try:
        assert await inbox.rebind_recovered_claims(records[0], lease) == 1
        after = (await receipts(member))[0]
        assert after.id == before.id and after.message_id == before.message_id
        assert after.generation == lease.generation and after.state == "claimed"
        async with get_db_session() as db:
            assert await db.scalar(select(func.count(Message.id)).where(Message.session_id == member, Message.role == "user")) == 1
        stale = replace(actor, kind="member", member_id=member, driver_run_id=records[0].run_id, generation=records[0].generation)
        try:
            await command(run, stale, "stale-command", {}, lambda writer: commands.queue_message(writer,
                to_member_id=writer.run.root_session_id, body="Must not be accepted"))
        except TeamError as exc:
            assert exc.code == "STALE_GENERATION"
        else:
            raise AssertionError("Old Driver generation retained write authority")
        await inbox.settle_claimed_inbox_items(lease, result_message_id=None, outcome="recovered")
    finally:
        await lease.release()


async def verify(selected=None):
    report = {}
    operations = ["command_response_lost", "provisioning_uncommitted", "queued_message", "inbox_before_receipt",
        "dispatch_before_wake", "claimed_before_driver", "member_result_before_notify", "coordinator_wait",
        "natural_answer", "paid_before_send", "external_receipt_lost", "corrupt_cache", "driver_capacity",
        "recovery_tick", "owner_message"]
    if selected:
        if set(selected) - set(operations) - {"cross_process_pause"}:
            raise ValueError("Unknown recovery scenario")
        operations = [operation for operation in operations if operation in selected]
    for operation in operations:
        run, actor, member = await seed()
        initial = await snapshot(run, actor)
        root = initial["run"]["root_session_id"]
        if operation == "inbox_before_receipt":
            await mail(run, actor, member)
        if operation == "recovery_tick":
            await mail(run, actor, member)
            await asyncio.gather(crash(operation, run), crash(operation, run))
        else:
            await crash(operation, run)
        if operation in {"command_response_lost", "provisioning_uncommitted"}:
            before = await snapshot(run, actor)
            assert len(before["members"]) == (3 if operation == "command_response_lost" else 2)
            one = await command(run, actor, "admit", {}, admit)
            two = await command(run, actor, "admit", {}, admit)
            assert one == two
            state = await snapshot(run, actor)
            assert len(state["members"]) == 3
            async with get_db_session() as db:
                assert await db.scalar(select(func.count(Session.id)).where(Session.parent_id == root)) == 2
        elif operation in {"queued_message", "inbox_before_receipt"}:
            assert not await receipts(member), "Inbox committed without the journal receipt"
            await scheduler.tick(run, actor)
            await scheduler.tick(run, actor)
            assert len([row for row in await receipts(member) if row.source_type == "team_message"]) == 1
            assert all(row["state"] == "delivered" for row in (await snapshot(run, actor))["messages"].values())
        elif operation == "dispatch_before_wake":
            for _ in range(2):
                await scheduler.tick(run, actor)
            assert member in _wakes
            assert len((await snapshot(run, actor))["attempts"]) == 1
            assert len(await receipts(member)) == 1
        elif operation == "claimed_before_driver":
            await recover_claim(run, actor, member)
            await scheduler.tick(run, actor)
            assert len((await snapshot(run, actor))["attempts"]) == 1
        elif operation in {"member_result_before_notify", "coordinator_wait"}:
            if operation == "coordinator_wait":
                assert (await snapshot(run, actor))["members"][root]["execution_state"] == "waiting"
                await result(run, actor)
            await scheduler.tick(run, actor)
            await scheduler.tick(run, actor)
            assert len(await receipts(root)) == 1 and root in _wakes
            assert all(row["state"] == "delivered" for row in (await snapshot(run, actor))["messages"].values())
        elif operation == "natural_answer":
            assert (await snapshot(run, actor))["members"][member]["nudged"] is True
            token = bind(run, actor, initial, member)
            lease = await driver.reserve_run(member, actor.owner_user_id)
            try:
                await lifecycle.turn_started(lease)
                await lifecycle.turn_ended(lease, text="Checked fixture result", outcome="succeeded")
            finally:
                await lease.release()
                runtime_binding._current.reset(token)
            state = await snapshot(run, actor)
            attempt = next(iter(state["attempts"].values()))
            assert attempt["implicit"] and attempt["state"] == "review"
            assert len([row for row in await receipts(member) if row.client_id.startswith("team:nudge:")]) == 1
        elif operation in {"paid_before_send", "external_receipt_lost"}:
            await recover_claim(run, actor, member)
            state = await snapshot(run, actor)
            reserved = next(iter(state["reservations"].values()))
            effect_id = reserved["external_id"]
            async with get_db_session() as db:
                effect = await db.get(ExternalEffect, effect_id)
                if effect.claim_kind is not None:
                    effect.claim_expires_at = utcnow() - timedelta(seconds=1)
                effect.prepared_at = utcnow() - timedelta(hours=1)
            await scheduler.tick(run, actor)
            assert next(iter((await snapshot(run, actor))["attempts"].values()))["state"] == "outcome_unknown"
            outcome = await effect_ledger.recover_effect_once(effect_id)
            await paid_tools.reconcile(run, actor)
            state = await snapshot(run, actor)
            if operation == "paid_before_send":
                assert outcome == "failed_before_dispatch"
                assert state["reservations"][reserved["id"]]["state"] == "released"
                assert not any(row["code"] == "FIXTURE_PROVIDER_CALLED" for row in state["notices"])
            else:
                assert outcome == "manual_review"
                assert state["reservations"][reserved["id"]]["state"] == "reserved"
                assert sum(row["code"] == "FIXTURE_PROVIDER_CALLED" for row in state["notices"]) == 1
                new_lease = await driver.reserve_run(member, actor.owner_user_id)
                try:
                    fence = effect_ledger.EffectRunFence(member, actor.owner_user_id, new_lease.run_id, new_lease.generation)
                    try:
                        await effect_ledger.claim_effect_for_dispatch(effect_id, fence)
                    except effect_ledger.EffectNotDispatchableError:
                        pass
                    else:
                        raise AssertionError("Unknown provider request was redispatched")
                finally:
                    await new_lease.release()
        elif operation == "corrupt_cache":
            assert await snapshot(run, actor) == initial
            await mail(run, actor, member)
            async with get_db_session() as db:
                assert (await db.get(TeamRun, run)).state_cache.get("cache_format_version") == 2
        elif operation == "driver_capacity":
            state = await snapshot(run, actor)
            assert state["run"]["capacity_failures"] == 1
            assert not await scheduler.can_wake(member, actor.owner_user_id)
            occupant = next(row["member_id"] for row in state["notices"] if row["code"] == "FIXTURE_QUOTA_OCCUPANT")
            await expire_fixture(occupant)
            expired = next(row for row in await driver.recover_expired_driver_records() if row.session_id == occupant)
            recovered = await driver.reserve_recovered_run(expired, initial_phase="reserved")
            await recovered.release()
            original_clock = capacity.utcnow
            capacity.utcnow = lambda: original_clock() + timedelta(seconds=11)
            try:
                lease, batch = await inbox._reserve_and_claim(member, actor.owner_user_id)
                assert len(batch.receipts) == 1 and len(await receipts(member)) == 1
                token = bind(run, actor, state, member)
                try:
                    await lifecycle.turn_started(lease)
                    assert (await snapshot(run, actor))["run"]["capacity_retry_at"] is None
                    await inbox.settle_claimed_inbox_items(lease, result_message_id=None, outcome="fixture")
                finally:
                    await lease.release()
                    runtime_binding._current.reset(token)
            finally:
                capacity.utcnow = original_clock
            assert len((await snapshot(run, actor))["attempts"]) == 1
        elif operation == "recovery_tick":
            await scheduler.tick(run, actor)
            state = await snapshot(run, actor)
            assert len(state["attempts"]) == 1
            assert len(await receipts(member)) == 2  # One task and one peer input.
            assert all(row["state"] == "delivered" for row in state["messages"].values())
        elif operation == "owner_message":
            from team.owner_messages import OwnerMessage, send
            before = await snapshot(run, actor)
            first = await send(run, replace(actor, kind="user"), "owner-correction", OwnerMessage(text="Keep this original user correction."))
            assert await snapshot(run, actor) == before
            rows = await receipts(root)
            assert len(rows) == 1 and rows[0].id == first["id"] and rows[0].source_type is None
            assert rows[0].prompt == "Keep this original user correction."
            await scheduler.tick(run, actor)
            assert root in _wakes
        state = await assert_consistent(run, actor)
        report[operation] = {"status": "passed", "run_id": run, "seq": state["seq"]}
        print(json.dumps({"checkpoint": operation, **report[operation]}), flush=True)
    if not selected or "cross_process_pause" in selected:
        report["cross_process_pause"] = await verify_remote_pause()
    print(json.dumps({"database": "PostgreSQL", "executor": "recording fixture; no model/tool provider calls",
        "crash_method": "os._exit(73)", "results": report}, indent=2))


async def verify_remote_pause():
    from team.service import control
    run, actor, member = await seed()
    process = await asyncio.create_subprocess_exec(sys.executable, __file__, "--child", "active_execution", "--run", run,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    async with asyncio.timeout(20):
        ready = json.loads(await process.stdout.readline())
    assert ready["ready"] and ready["member"] == member
    state = await snapshot(run, actor)
    await control(run, replace(actor, kind="user"), "pause-cross-process", "pause", state["run"]["revision"])
    await scheduler.tick(run, actor)
    async with asyncio.timeout(25):
        output, error = await process.communicate()
    assert process.returncode == 0, (output.decode()[-1000:], error.decode()[-4000:])
    await scheduler.tick(run, actor)
    state = await assert_consistent(run, actor)
    assert state["run"]["state"] == "paused"
    assert not any(attempt["state"] == "running" for attempt in state["attempts"].values())
    async with get_db_session() as db:
        assert not (await runtime.runtime.observe(db, [member], actor.owner_user_id))[member].live
    return {"status": "passed", "run_id": run, "seq": state["seq"], "observed_generation": ready["generation"]}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--child")
    parser.add_argument("--run")
    parser.add_argument("--scenarios", help="Comma-separated scenarios; omitted runs the whole recovery matrix")
    args = parser.parse_args()
    url = make_url(os.environ.get("TEAM_TEST_DATABASE_URL", "sqlite://"))
    if url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost"} or not any(word in (url.database or "") for word in ("test", "check")):
        raise SystemExit("TEAM_TEST_DATABASE_URL must identify a migrated loopback PostgreSQL test/check database")
    configure()
    init_engine(url.render_as_string(hide_password=False))
    try:
        await child(args.child, args.run) if args.child else await verify(args.scenarios.split(",") if args.scenarios else None)
    finally:
        await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
