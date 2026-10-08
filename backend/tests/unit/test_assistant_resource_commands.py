"""SQL commands -> real client/auth HTTP -> independent durable remote journal."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import func, select, update

from api.assistant import router
from assistant import resource_commands as commands
from assistant.policy import AssistantError
from assistant.retry import read_command
from assistant.service import get_main_session
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from db.base import get_db_session
from db.models.assistant import AssistantCommand
from db.models.external_effect import ExternalEffect
from db.models.resource_control import ResourceControlLease
from db.models.workspace import WorkspaceMember
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_resource_control import resource, prepare  # noqa: F401
from tests.unit.test_assistant_resource_gateway import gateway, invocation, new_computer_call  # noqa: F401
from tests.unit.test_action_server_desktop_lease import server
from resource_gate import ResourceGate
from sandbox.resource_operation import prepare_desktop_tool


@pytest.fixture
async def remote(resource, gateway, tmp_path, monkeypatch):
    ctx, _, _ = gateway
    journal = ResourceGate(tmp_path / "remote.sqlite3")
    monkeypatch.setattr(server, "_resource_gate", journal)
    monkeypatch.setattr(server, "SESSION_API_KEY", "fixture-key")
    monkeypatch.setattr(server, "_desktop_lease", None)
    ctx.sandbox._transport = httpx.ASGITransport(app=server.app)
    scope = {"user_id": ctx.user_id, "workspace_id": ctx.workspace_id}
    scope["main_id"] = (await get_main_session(**scope)).id

    @asynccontextmanager
    async def factory(_desktop):
        yield ctx.sandbox

    monkeypatch.setattr(commands, "remote_client", factory)
    return scope, journal, ctx


async def accept(remote, resource, action, key=None):
    return await commands.accept_resource_command(**remote[0], resource_id=resource[0].resource_id,
        expected_epoch=resource[0].epoch, idempotency_key=key or action, action=action)


async def test_concurrent_bind_receipts_pin_one_journal_and_replay_across_server_restart(remote, resource):
    left, right = await asyncio.gather(*(accept(remote, resource, "bind") for _ in range(2)))
    assert left == right
    await asyncio.gather(*(commands.dispatch(left["command_id"]) for _ in range(2)))
    saved = await read_command(**remote[0], command_id=left["command_id"])
    assert saved["state"] == "applied"
    assert saved["receipt"]["remote_journal_id"] == remote[1].status()["journal_id"]
    reopened = ResourceGate(remote[1].path)
    assert reopened.command_receipt(left["command_id"]) == saved["receipt"]["remote_receipt"]
    assert await accept(remote, resource, "bind") == saved["receipt"]
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(
            AssistantCommand.target_id == resource[0].resource_id)) == 1
    result = await commands.read_resource(**remote[0], resource_id=resource[0].resource_id)
    assert result["admission_state"] == "open"
    assert result["human_takeover_available"] is False and result["remote_exclusivity_verified"] is False


async def test_close_commits_before_io_and_lost_response_recovers_same_command_after_restart(remote, resource, monkeypatch):
    from agent import effect_ledger as effects
    prepared = await prepare(resource)
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, resource[1])
    accepted = await accept(remote, resource, "close")
    assert remote[1].status()["control"] is None  # No network inside acceptance.
    with pytest.raises(AssistantError):
        await effects.mark_effect_submitting(claim)
    actual = remote[2].sandbox.resource_command

    async def lost(*args):
        await actual(*args)
        raise httpx.ReadTimeout("synthetic lost close response")

    monkeypatch.setattr(remote[2].sandbox, "resource_command", lost)
    assert not await commands.dispatch(accepted["command_id"])
    assert remote[1].status()["control"]["admission"] == "closed"
    saved = await read_command(**remote[0], command_id=accepted["command_id"])
    assert saved["state"] == "applying" and saved["receipt"]["remote_journal_id"] == remote[1].status()["journal_id"]
    monkeypatch.setattr(server, "_resource_gate", ResourceGate(remote[1].path))
    monkeypatch.setattr(remote[2].sandbox, "resource_command", actual)
    async with get_db_session() as db:
        row = await db.get(AssistantCommand, accepted["command_id"])
        row.updated_at -= timedelta(seconds=30)
    assert await commands.recover_resource_commands() == 1
    saved = await read_command(**remote[0], command_id=accepted["command_id"])
    assert saved["state"] == "applied" and "error_code" not in saved["receipt"]
    assert saved["receipt"]["remote_receipt"]["admission"] == "closed"
    with remote[1].transaction() as db:
        assert db.execute("SELECT count(*) FROM control_commands").fetchone()[0] == 1
    result = await commands.read_resource(**remote[0], resource_id=resource[0].resource_id)
    assert result["tracked_operations_drained"] and not result["remote_exclusivity_verified"]


async def test_lost_bind_reply_keeps_operations_closed_until_the_original_receipt_is_recovered(remote, resource, monkeypatch):
    accepted = await accept(remote, resource, "bind")
    actual = remote[2].sandbox.resource_command

    async def lost(*args):
        await actual(*args)
        raise httpx.ReadTimeout("synthetic lost bind response")

    monkeypatch.setattr(remote[2].sandbox, "resource_command", lost)
    assert not await commands.dispatch(accepted["command_id"])
    assert (await invocation(remote[2])).metadata["error"]
    assert remote[1].status()["blocking_count"] == 0
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(ExternalEffect).where(
            ExternalEffect.session_id == remote[2].session_id)) == 0
    monkeypatch.setattr(remote[2].sandbox, "resource_command", actual)
    assert await commands.dispatch(accepted["command_id"])
    assert (await commands.read_resource(**remote[0], resource_id=resource[0].resource_id))["remote_status"]["control"]["admission"] == "open"


@pytest.mark.parametrize("prepare_first", [False, True])
async def test_old_call_cannot_adopt_a_journal_bound_after_its_model_request(remote, resource, prepare_first):
    ctx = remote[2]
    if prepare_first:
        prepared, _, journal_id = await prepare_desktop_tool(ctx, {"action": "screenshot"})
        assert journal_id is None
    accepted = await accept(remote, resource, "bind")
    assert await commands.dispatch(accepted["command_id"])
    assert (await invocation(ctx)).metadata["error"]
    assert remote[1].status()["blocking_count"] == 0
    async with get_db_session() as db:
        rows = list((await db.scalars(select(ExternalEffect).where(ExternalEffect.session_id == ctx.session_id))).all())
        if prepare_first:
            row, = rows
            assert row.id == prepared.snapshot.effect_id and row.state == "prepared" and row.attempt_count == 0
            assert row.safe_context["resource_journal_id"] is None
        else:
            assert not rows


@pytest.mark.parametrize("already_submitting", [False, True])
async def test_journal_change_after_claim_blocks_the_ledger_send_boundary(remote, resource, already_submitting):
    from agent import effect_ledger as effects
    prepared, _, _ = await prepare_desktop_tool(remote[2], {"action": "screenshot"})
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, resource[1])
    if already_submitting:
        await effects.mark_effect_submitting(claim)
    accepted = await accept(remote, resource, "bind")
    assert await commands.dispatch(accepted["command_id"])

    with pytest.raises(AssistantError):
        if already_submitting:
            await effects.assert_effect_dispatchable(claim)
        else:
            await effects.mark_effect_submitting(claim)

    async with get_db_session() as db:
        row = await db.get(ExternalEffect, prepared.snapshot.effect_id)
        assert row.safe_context["resource_journal_id"] is None
        assert row.state == ("submitting" if already_submitting else "prepared")
        assert row.attempt_count == int(already_submitting)
    assert remote[1].status()["blocking_count"] == 0


async def test_replaced_journal_between_read_and_write_is_never_adopted(remote, resource, tmp_path, monkeypatch):
    accepted = await accept(remote, resource, "bind")
    original = remote[2].sandbox.resource_command
    replacement = ResourceGate(tmp_path / "replacement.sqlite3")

    async def replaced(action, payload):
        async with get_db_session() as db:
            row = await db.get(ResourceControlLease, resource[0].resource_id)
            assert row.remote_journal_id == remote[1].status()["journal_id"] == payload["journal_id"]
        monkeypatch.setattr(server, "_resource_gate", replacement)
        return await original(action, payload)

    monkeypatch.setattr(remote[2].sandbox, "resource_command", replaced)
    assert not await commands.dispatch(accepted["command_id"])
    assert replacement.status()["control"] is None and replacement.status()["blocking_count"] == 0
    saved = await read_command(**remote[0], command_id=accepted["command_id"])
    assert saved["state"] == "blocked"
    result = await commands.read_resource(**remote[0], resource_id=resource[0].resource_id)
    assert result["status"] == "hold" and result["admission_state"] == "closed"
    assert result["remote_journal_id"] == remote[1].status()["journal_id"]


async def test_pinned_computer_operation_rejects_an_empty_replacement_before_any_process(remote, resource, tmp_path, monkeypatch):
    accepted = await accept(remote, resource, "bind")
    assert await commands.dispatch(accepted["command_id"])
    await new_computer_call(remote[2])
    replacement = ResourceGate(tmp_path / "empty.sqlite3")
    monkeypatch.setattr(server, "_resource_gate", replacement)

    async def forbidden(*args, **kwargs):
        pytest.fail("a replaced journal launched a process")

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", forbidden)
    assert (await invocation(remote[2])).metadata["error"]
    assert replacement.status()["blocking_count"] == 0
    async with get_db_session() as db:
        effect = await db.scalar(select(ExternalEffect).where(ExternalEffect.session_id == remote[2].session_id))
        assert effect.state == "outcome_unknown"


async def test_delayed_bind_response_cannot_reopen_or_replace_a_closed_snapshot(remote, resource, monkeypatch):
    accepted = await accept(remote, resource, "bind")
    original = remote[2].sandbox.resource_command
    received, release = asyncio.Event(), asyncio.Event()

    async def delayed(action, payload):
        result = await original(action, payload)
        if action == "bind":
            received.set()
            await asyncio.wait_for(release.wait(), 5)
        return result

    monkeypatch.setattr(remote[2].sandbox, "resource_command", delayed)
    binding = asyncio.create_task(commands.dispatch(accepted["command_id"]))
    try:
        await asyncio.wait_for(received.wait(), 5)
        closing = await accept(remote, resource, "close")
        assert await commands.dispatch(closing["command_id"])
    finally:
        release.set()
        await binding
    result = await commands.read_resource(**remote[0], resource_id=resource[0].resource_id)
    assert result["admission_state"] == result["remote_status"]["control"]["admission"] == "closed"
    assert remote[1].status()["control"]["admission"] == "closed"


async def test_revoked_actor_cannot_dispatch_and_stale_keys_do_not_select_new_fences(remote, resource):
    accepted = await accept(remote, resource, "bind")
    with pytest.raises(AssistantError, match="inspection"):
        await accept(remote, resource, "close", key="bind")
    with pytest.raises(AssistantError) as stale:
        await commands.accept_resource_command(**remote[0], resource_id=resource[0].resource_id,
            action="close", expected_epoch=2, idempotency_key="stale")
    assert stale.value.code == "RESOURCE_FENCE_CHANGED"
    async with get_db_session() as db:
        await db.execute(update(WorkspaceMember).where(WorkspaceMember.user_id == remote[0]["user_id"]).values(status="removed"))
    assert not await commands.dispatch(accepted["command_id"])
    assert remote[1].status()["control"] is None
    with pytest.raises(AssistantError):
        await read_command(**remote[0], command_id=accepted["command_id"])


@pytest.mark.parametrize("changed", ["command_id", "journal_id", "epoch", "action", "admission"])
async def test_mismatched_command_receipt_never_certifies_applied(remote, resource, monkeypatch, changed):
    accepted = await accept(remote, resource, "close")
    actual = remote[2].sandbox.resource_command

    async def wrong(*args):
        result = await actual(*args)
        result["command_receipt"][changed] = "wrong"
        return result

    monkeypatch.setattr(remote[2].sandbox, "resource_command", wrong)
    assert not await commands.dispatch(accepted["command_id"])
    result = await read_command(**remote[0], command_id=accepted["command_id"])
    assert result["state"] == "blocked" and result["receipt"]["error_code"] == "RESOURCE_RECEIPT_INVALID"


async def test_authenticated_http_control_and_command_reads_share_the_durable_service(remote, resource):
    app = FastAPI()
    app.include_router(router)
    actor = {key: remote[0][key] for key in ("user_id", "workspace_id")}
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_workspace] = lambda: actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        url = f"/api/assistant/resources/{resource[0].resource_id}"
        response = await client.post(url + "/control", json={"action": "close", "expected_epoch": 1, "idempotency_key": "http-close"})
        assert response.status_code == 202
        command = await client.get("/api/assistant/commands/" + response.json()["command_id"])
        assert command.status_code == 200 and command.json()["state"] == "applied"
        status = await client.get(url)
        assert status.json()["admission_state"] == "closed" and status.json()["human_takeover_available"] is False
        unsupported = await client.post(url + "/control", json={"action": "giveback", "expected_epoch": 1, "idempotency_key": "unsupported"})
        assert unsupported.status_code == 422


async def test_unsupported_remote_before_any_write_does_not_pin_or_change_existing_admission(remote, resource, monkeypatch):
    accepted = await accept(remote, resource, "bind")

    async def legacy():
        return {**remote[1].status(), "protocol": "resource_admission_v1"}

    monkeypatch.setattr(remote[2].sandbox, "resource_status", legacy)
    assert not await commands.dispatch(accepted["command_id"])
    result = await commands.read_resource(**remote[0], resource_id=resource[0].resource_id)
    assert result["remote_journal_id"] is None and result["admission_state"] == "open"
    assert remote[1].status()["control"] is None
