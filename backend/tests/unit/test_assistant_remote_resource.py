"""Real SQL effect -> SandboxClient -> Action Server journal -> local process."""
from dataclasses import replace
from datetime import timedelta
import pytest

from agent import effect_ledger as effects
from db.base import get_db_session
from db.models.external_effect import ExternalEffect, ExternalEffectEvidence
from sqlalchemy import select
from tool import computer
from tool.tool import ToolResult
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_resource_control import resource, prepare, close  # noqa: F401
from tests.unit.test_assistant_resource_gateway import gateway, invocation, new_computer_call  # noqa: F401
from tests.unit.test_action_server_desktop_lease import server
from resource_gate import Fence
from question import runtime
from tests.unit.test_assistant_resource_commands import remote, accept  # noqa: F401
from assistant import resource_commands


async def test_remote_close_fences_a_late_request_even_when_backend_admission_is_still_open(
    resource, remote, tmp_path, monkeypatch,
):
    _, journal, ctx = remote
    control = resource[0]
    fence = Fence(control.resource_id, control.epoch, control.owner_kind, control.owner_id)
    binding = await accept(remote, resource, "bind")
    assert await resource_commands.dispatch(binding["command_id"])
    await new_computer_call(ctx)
    actual_processes = []
    create = server.asyncio.create_subprocess_exec

    async def counted(*command, **kwargs):
        actual_processes.append(command)
        return await create(*command, **kwargs)

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", counted)

    async def compound(_args, context):
        result = await context.sandbox.execute("printf remote-fixture", workdir=str(tmp_path))
        assert result.exit_code == 0 and result.stdout == "remote-fixture"
        journal.close(fence, "fixture-close", journal.status()["journal_id"])
        await context.sandbox.execute("printf must-not-run", workdir=str(tmp_path))
        return ToolResult(output="unreachable")

    monkeypatch.setattr(computer, "_execute_locked", compound)
    result = await invocation(ctx)
    assert result.metadata["resource_outcome"] == "outcome_unknown"
    assert actual_processes == [("/bin/sh", "-c", "printf remote-fixture")]
    assert server._desktop_lease is None  # Exact-token cleanup remains available.
    async with get_db_session() as db:
        effect = await db.scalar(select(ExternalEffect).where(ExternalEffect.session_id == ctx.session_id))
        assert effect.state == "outcome_unknown"
        remote = journal.status()
        assert remote["blocking_count"] == 1  # Only opaque execute, not lock acquisition.
        assert remote["blocking_operations"][0]["effect_id"] == effect.id
        assert remote["control"]["admission"] == "closed"
        assert not remote["remote_exclusivity_verified"]
        receipts = list((await db.scalars(select(ExternalEffectEvidence).where(
            ExternalEffectEvidence.effect_id == effect.id,
            ExternalEffectEvidence.phase == "resource.admitted"))).all())
        assert len(receipts) == 2  # Remote lease acquisition and first execute.
        assert {r.evidence["remote_journal_id"] for r in receipts} == {remote["journal_id"]}
        for row in receipts:
            saved = journal.receipt(row.evidence["remote_operation_id"])
            assert saved["effect_id"] == effect.id


async def test_late_remote_receipt_retains_evidence_without_granting_stale_workers_authority(resource):
    prepared = await prepare(resource)
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, resource[1])
    await effects.mark_effect_submitting(claim)
    await close(resource)
    await resource[2].release(session_status="idle")
    evidence = {"remote_operation_id": "fixture-step", "remote_journal_id": "a" * 32}
    await effects.record_effect_dispatch_progress(claim, phase="resource.admitted", evidence=evidence)
    with pytest.raises(effects.EffectLeaseLostError):
        await effects.record_effect_dispatch_progress(replace(claim, token="stale"),
            phase="resource.admitted", evidence=evidence)
    async with get_db_session() as db:
        row = await db.get(ExternalEffect, claim.effect_id)
        assert row.state == "submitting"
        row.claim_expires_at = runtime.now() - timedelta(seconds=2)
    with pytest.raises(effects.EffectLeaseLostError):
        await effects.record_effect_dispatch_progress(claim, phase="resource.admitted", evidence=evidence)
    async with get_db_session() as db:
        receipts = list((await db.scalars(select(ExternalEffectEvidence).where(
            ExternalEffectEvidence.effect_id == claim.effect_id,
            ExternalEffectEvidence.phase == "resource.admitted"))).all())
        assert len(receipts) == 1
