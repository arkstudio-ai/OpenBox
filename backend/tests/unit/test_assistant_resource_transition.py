"""Real SQL Command recovery plus actual authenticated loopback Action Server."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text

from agent import effect_ledger as effects
from api.assistant import ResourceCommandBody
from assistant import resource_commands as commands, resource_control as controls
from assistant.policy import AssistantError
from assistant.retry import read_command
from assistant.service import ensure_main_session, get_main_session
from billing.plans import plan_catalog
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.assistant import AssistantCommand
from db.models.billing import BillingSubscription, PaymentOrder
from db.models.external_effect import ExternalEffect
from db.models.resource_control import ResourceControlLease
from db.models.workspace import WorkspaceMember
from sandbox.client import SandboxClient
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_resource_control import resource, prepare  # noqa: F401
from tests.unit.test_action_server_resource_transition import gate, listening, server  # noqa: F401
from resource_gate import Fence, ResourceGate


@pytest.fixture
async def remote(resource, gate, monkeypatch):
    scope = {key: resource[3][key] for key in ("user_id", "workspace_id")}
    scope["main_id"] = (await get_main_session(**scope)).id
    # The real SandboxClient hook still checks actual paid-access SQL.
    async with get_db_session() as db:
        stamp, order_id = await controls.clock(db), uuid4().hex
        plan = plan_catalog().plan("pro")
        db.add(PaymentOrder(id=order_id, workspace_id=scope["workspace_id"], user_id=scope["user_id"],
            provider="local-fixture", request_key=order_id, amount_fen=1, credits=1,
            kind="subscription", status="paid", created_at=stamp, paid_at=stamp))
        await db.flush()
        db.add(BillingSubscription(order_id=order_id, workspace_id=scope["workspace_id"],
            plan_id=plan.id, cycle="monthly", plan=plan.model_dump(mode="json"),
            starts_at=stamp-timedelta(minutes=1), ends_at=stamp+timedelta(days=1)))
    async with listening(server.app) as port:
        client = SandboxClient("127.0.0.1", port, "fixture-key", desktop_id=resource[3]["desktop_id"],
            workspace_id=scope["workspace_id"])
        @asynccontextmanager
        async def factory(desktop):
            assert desktop.desktop_id == resource[3]["desktop_id"]
            yield client
        monkeypatch.setattr(commands, "remote_client", factory)
        try:
            yield scope, gate, client
        finally:
            await client.aclose()


async def accepted(remote, resource, key="transition", epoch=1):
    return await commands.accept_closed_transition(**remote[0], resource_id=resource[0].resource_id,
        expected_epoch=epoch, idempotency_key=key)


async def state(resource):
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, resource[0].resource_id)
        return {"fence": asdict(controls.fence_for(row)), "status": row.status,
            "admission": row.admission_state, "observation": row.last_observation_ref,
            "journal_id": row.remote_journal_id, "expires_at": row.expires_at}


async def test_closed_roundtrip_retires_prepared_effect_and_never_grants_input(remote, resource):
    prepared = await prepare(resource)
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, resource[1])
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, resource[0].resource_id)
        row.last_observation_ref = {"old_frame": "fixture"}
    first = await accepted(remote, resource)
    assert (await state(resource))["admission"] == "closed"
    with pytest.raises(AssistantError):
        await effects.mark_effect_submitting(claim)
    assert await commands.dispatch(first["command_id"])
    human = await state(resource)
    assert human["fence"] == first["next_fence"]
    assert human["status"] == "hold" and human["observation"] is None
    assert human["admission"] == "closed" and human["expires_at"] is not None
    assert (await accepted(remote, resource)) == (await read_command(**remote[0], command_id=first["command_id"]))["receipt"]
    second = await accepted(remote, resource, "giveback-preparation", 2)
    assert await commands.dispatch(second["command_id"])
    after = await state(resource)
    assert after["fence"]["epoch"] == 3 and after["fence"]["owner_kind"] == "automation"
    assert after["status"] == "hold" and after["admission"] == "closed"
    assert after["observation"] is None and after["expires_at"] is None
    assert remote[1].status()["control"]["epoch"] == 3
    assert remote[1].status()["remote_exclusivity_verified"] is False
    with pytest.raises(ValidationError):
        ResourceCommandBody(action="advance_closed", expected_epoch=3, idempotency_key="not-a-product-action")


async def another_scope(remote):
    async with get_db_session() as db:
        other = await db.scalar(select(WorkspaceMember.user_id).where(
            WorkspaceMember.workspace_id == remote[0]["workspace_id"],
            WorkspaceMember.user_id != remote[0]["user_id"]))
    scope = {"user_id": other, "workspace_id": remote[0]["workspace_id"]}
    scope["main_id"] = (await ensure_main_session(**scope, model="fixture/local")).id
    return scope


async def test_independent_sql_acceptance_allows_one_source_epoch_command(remote, resource, monkeypatch, record_property):
    other = await another_scope(remote)
    hold, release = asyncio.Event(), asyncio.Event()
    pids = []
    actual = controls.locked
    postgres = get_engine().dialect.name == "postgresql"
    async def locked(db, resource_id):
        pid = await db.scalar(text("SELECT pg_backend_pid()"))
        if pid not in pids:
            pids.append(pid)
        row = await actual(db, resource_id)
        if pid == pids[0] and not hold.is_set():
            hold.set()
            await asyncio.wait_for(release.wait(), 5)
        return row
    if postgres:
        monkeypatch.setattr(controls, "locked", locked)
    left = asyncio.create_task(accepted(remote, resource, "left"))
    right = None
    try:
        if postgres:
            await asyncio.wait_for(hold.wait(), 3)
        right = asyncio.create_task(commands.accept_closed_transition(**other,
            resource_id=resource[0].resource_id, expected_epoch=1, idempotency_key="right"))
        if postgres:
            # A third connection must observe the second actor truly waiting
            # on the production resource row lock, not a mocked mutex.
            async with asyncio.timeout(3):
                while True:
                    if len(pids) > 1:
                        async with get_db_session() as db:
                            witness = (await db.execute(text("SELECT pid,wait_event_type,pg_blocking_pids(pid) AS blockers "
                                "FROM pg_stat_activity WHERE pid=:pid"), {"pid": pids[1]})).mappings().one()
                        if witness["wait_event_type"] == "Lock" and pids[0] in witness["blockers"]:
                            record_property("resource_transition_lock_witness", str(dict(witness)))
                            break
                    assert not right.done(), "A competing actor bypassed the held resource row lock"
                    await asyncio.sleep(.01)
        release.set()
        outcomes = await asyncio.gather(left, right, return_exceptions=True)
    finally:
        release.set()
        await asyncio.gather(*(task for task in (left, right) if task is not None), return_exceptions=True)
    winner, = [item for item in outcomes if isinstance(item, dict)]
    loser, = [item for item in outcomes if isinstance(item, AssistantError)]
    assert loser.code == "RESOURCE_TRANSITION_PENDING"
    assert await commands.dispatch(winner["command_id"])
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(
            AssistantCommand.target_id == resource[0].resource_id,
            AssistantCommand.action == "resource_advance_closed")) == 1


async def test_same_key_replays_after_epoch_change_but_changed_payload_conflicts(remote, resource):
    left, right = await asyncio.gather(accepted(remote, resource), accepted(remote, resource))
    assert left == right
    assert await commands.dispatch(left["command_id"])
    assert (await accepted(remote, resource))["command_id"] == left["command_id"]
    with pytest.raises(AssistantError) as conflict:
        await accepted(remote, resource, epoch=2)
    assert conflict.value.code == "ASSISTANT_COMMAND_CONFLICT"


async def test_closed_human_preparation_cannot_be_renewed_or_returned_by_another_actor(remote, resource):
    command = await accepted(remote, resource)
    assert await commands.dispatch(command["command_id"])
    heartbeat = await controls.heartbeat(fence=controls.ResourceFence(**command["next_fence"]),
        user_id=remote[0]["user_id"])
    assert heartbeat["status"] == "hold" and heartbeat["admission_state"] == "closed"
    with pytest.raises(AssistantError):
        await commands.accept_closed_transition(**await another_scope(remote), resource_id=resource[0].resource_id,
            expected_epoch=2, idempotency_key="other-actor-giveback")
    assert (await state(resource))["fence"]["epoch"] == 2


async def test_inflight_sql_effect_stays_blocking_until_actual_terminal_settlement(remote, resource):
    prepared = await prepare(resource)
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, resource[1])
    await effects.mark_effect_submitting(claim)
    command = await accepted(remote, resource)
    assert not await commands.dispatch(command["command_id"])
    saved = await read_command(**remote[0], command_id=command["command_id"])
    assert saved["state"] == "applying" and saved["receipt"]["wait_reason"] == "RESOURCE_LOCAL_DRAINING"
    assert remote[1].status()["control"]["epoch"] == 1
    await effects.settle_effect(claim, state="succeeded", receipt={"fixture_confirmed_terminal": True})
    assert await commands.dispatch(command["command_id"])
    assert (await state(resource))["fence"]["epoch"] == 2


async def test_unknown_remote_shell_never_becomes_drained_after_reopen(remote, resource, tmp_path):
    # Legacy work is tracked before enrollment and must survive later binding.
    async with httpx.AsyncClient(base_url=remote[2].base_url, trust_env=False,
            headers={"X-API-Key": "fixture-key"}) as client:
        response = await client.post("/execute", json={"command": "printf local-only", "workdir": str(tmp_path)})
        assert response.json()["stdout"] == "local-only"
    command = await accepted(remote, resource)
    for _ in range(2):
        assert not await commands.dispatch(command["command_id"])
    saved = await read_command(**remote[0], command_id=command["command_id"])
    assert saved["state"] == "applying" and saved["receipt"]["wait_reason"] == "RESOURCE_REMOTE_DRAINING"
    assert ResourceGate(remote[1].path).status()["blocking_count"] == 1
    assert (await state(resource))["fence"]["epoch"] == 1


async def test_lost_remote_advance_response_recovers_original_command_after_sql_reopen(remote, resource, monkeypatch):
    command = await accepted(remote, resource)
    original = remote[2].resource_command
    async def lose(action, payload):
        result = await original(action, payload)
        if action == "advance_closed":
            raise httpx.ReadTimeout("fixture response discarded after remote commit")
        return result
    monkeypatch.setattr(remote[2], "resource_command", lose)
    assert not await commands.dispatch(command["command_id"])
    assert (await state(resource))["fence"]["epoch"] == 1
    assert remote[1].status()["control"]["epoch"] == 2
    url = get_engine().url
    await close_engine()
    init_engine(url)
    monkeypatch.setattr(server, "_resource_gate", ResourceGate(remote[1].path))
    monkeypatch.setattr(remote[2], "resource_command", original)
    async with get_db_session() as db:
        row = await db.get(AssistantCommand, command["command_id"])
        row.updated_at -= timedelta(seconds=30)
    assert await commands.recover_resource_commands() == 1
    assert (await state(resource))["fence"]["epoch"] == 2
    saved = await read_command(**remote[0], command_id=command["command_id"])
    assert saved["state"] == "applied" and saved["receipt"]["remote_receipt"]["command_id"] == command["command_id"]
    with remote[1].transaction() as db:
        assert db.execute("SELECT count(*) FROM control_commands WHERE action='advance_closed'").fetchone()[0] == 1


async def test_same_remote_owner_epoch_without_this_commands_receipt_cannot_be_adopted(remote, resource):
    command = await accepted(remote, resource)
    pinned = remote[1].status()["journal_id"]
    old = Fence(**asdict(resource[0]))
    remote[1].close(old, "independent-close", pinned)
    remote[1].advance_closed(old, Fence(**command["next_fence"]), "independent-transition", pinned)
    assert not await commands.dispatch(command["command_id"])
    saved = await read_command(**remote[0], command_id=command["command_id"])
    assert saved["state"] == "blocked"
    assert (await state(resource))["fence"]["epoch"] == 1


async def test_replacement_remote_journal_cannot_complete_a_pinned_transition(remote, resource, monkeypatch, tmp_path):
    command = await accepted(remote, resource)
    original = remote[2].resource_command
    replacement = ResourceGate(tmp_path / "replacement.sqlite3")
    async def replace_after_pin(action, payload):
        assert (await state(resource))["journal_id"] == remote[1].status()["journal_id"]
        monkeypatch.setattr(server, "_resource_gate", replacement)
        return await original(action, payload)
    monkeypatch.setattr(remote[2], "resource_command", replace_after_pin)
    assert not await commands.dispatch(command["command_id"])
    assert (await state(resource))["fence"]["epoch"] == 1
    assert replacement.status()["control"] is None


async def test_revoke_during_remote_commit_stops_final_sql_adoption(remote, resource, monkeypatch):
    command = await accepted(remote, resource)
    original = remote[2].resource_command
    async def revoke(action, payload):
        result = await original(action, payload)
        if action == "advance_closed":
            async with get_db_session() as db:
                member = await db.get(WorkspaceMember, (remote[0]["workspace_id"], remote[0]["user_id"]))
                member.status = "inactive"
        return result
    monkeypatch.setattr(remote[2], "resource_command", revoke)
    assert not await commands.dispatch(command["command_id"])
    assert remote[1].status()["control"]["epoch"] == 2
    assert remote[1].status()["control"]["admission"] == "closed"
    local = await state(resource)
    assert local["fence"]["epoch"] == 1 and local["status"] == "hold" and local["admission"] == "closed"
    async with get_db_session() as db:
        assert (await db.get(AssistantCommand, command["command_id"])).state == "blocked"
