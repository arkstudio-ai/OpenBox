"""Fault-injection coverage for the generic external-effect protocol."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql, sqlite

from agent import driver
import agent.effect_ledger as effects
from db.base import get_db_session
from db.models.external_effect import ExternalEffect
from db.models.project import Project
from db.models.session import Session
from db.models.user import User


async def _seed(prefix: str = "effect"):
    suffix = uuid4().hex[:12]
    user_id = f"{prefix}-user-{suffix}"
    project_id = f"{prefix}-project-{suffix}"
    session_id = f"{prefix}-session-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=user_id,
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name="Effect test",
            slug=f"effect-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Session(
            id=session_id,
            user_id=user_id,
            project_id=project_id,
            status="idle",
            created_at=now,
            updated_at=now,
        ))
    lease = await driver.reserve_run(session_id, user_id)
    fence = effects.EffectRunFence(
        session_id=session_id,
        tenant_id=user_id,
        run_id=lease.run_id,
        generation=lease.generation,
    )
    return user_id, project_id, session_id, lease, fence


async def _prepare(fence, project_id, *, adapter: str, logical_key: str = "call"):
    return await effects.prepare_effect(
        fence,
        adapter=adapter,
        provider="test-provider",
        operation="create",
        logical_key=logical_key,
        request_payload={"prompt": "你好😀", "size": "1024x1024"},
        project_id=project_id,
        safe_context={"text": "Unicode 路径/你好😀"},
    )


async def _expire_claim(effect_id: str) -> None:
    async with get_db_session() as db:
        await db.execute(
            update(ExternalEffect)
            .where(ExternalEffect.id == effect_id)
            .values(claim_expires_at=datetime.now(timezone.utc) - timedelta(seconds=2))
        )


@pytest.mark.asyncio
async def test_stable_effect_identity_rebinds_only_a_proven_pre_send_generation():
    _user, project, session, first, fence = await _seed("stable")
    prepared = await _prepare(fence, project, adapter="stable-adapter")
    duplicate = await _prepare(fence, project, adapter="stable-adapter")
    assert duplicate.snapshot.effect_id == prepared.snapshot.effect_id
    assert duplicate.snapshot.idempotency_key == prepared.snapshot.idempotency_key
    assert duplicate.created is False

    abandoned = await effects.claim_effect_for_dispatch(
        prepared.snapshot.effect_id, fence
    )
    await effects.abandon_effect_before_dispatch(
        abandoned, reason="injected_pre_send_stop"
    )

    assert await first.release(session_status="idle") is True
    second = await driver.reserve_run(session, fence.tenant_id)
    second_fence = effects.EffectRunFence(
        session, fence.tenant_id, second.run_id, second.generation
    )
    rebound = await _prepare(second_fence, project, adapter="stable-adapter")
    assert rebound.snapshot.effect_id == prepared.snapshot.effect_id
    assert rebound.snapshot.run_generation == second.generation

    claim = await effects.claim_effect_for_dispatch(
        rebound.snapshot.effect_id, second_fence
    )
    await effects.settle_effect(
        claim,
        state="failed",
        error={"code": "test_cleanup_before_dispatch"},
    )
    await second.release(session_status="idle")


@pytest.mark.asyncio
async def test_same_logical_identity_rejects_request_hash_drift():
    _user, project, _session, lease, fence = await _seed("hash-drift")
    prepared = await _prepare(fence, project, adapter="hash-drift-adapter")
    with pytest.raises(effects.EffectConflictError):
        await effects.prepare_effect(
            fence,
            adapter="hash-drift-adapter",
            provider="test-provider",
            operation="create",
            logical_key="call",
            request_payload={"prompt": "different"},
        )
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, fence)
    await effects.settle_effect(claim, state="failed", error={"code": "cleanup"})
    await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_dispatch_guard_after_send_marker_makes_zero_external_calls_when_run_is_lost():
    _user, project, _session, lease, fence = await _seed("before-send")
    prepared = await _prepare(fence, project, adapter="before-send-adapter")
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, fence)
    await effects.mark_effect_submitting(claim)
    assert await lease.release(session_status="idle") is True

    provider_calls = 0
    with pytest.raises(driver.LeaseLostError):
        await effects.assert_effect_dispatchable(claim)
        provider_calls += 1
    assert provider_calls == 0

    await _expire_claim(prepared.snapshot.effect_id)
    assert await effects.recover_effect_once(prepared.snapshot.effect_id) == "manual_review"
    assert (await effects.get_effect(prepared.snapshot.effect_id)).state == "manual_review"


@pytest.mark.asyncio
async def test_provider_response_lost_before_receipt_commit_is_never_redispatched():
    _user, project, _session, lease, fence = await _seed("response-loss")
    prepared = await _prepare(fence, project, adapter="no-query-adapter")
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, fence)
    await effects.mark_effect_submitting(claim)
    await effects.assert_effect_dispatchable(claim)

    # Fault boundary: provider returned, process died before receipt commit.
    provider_calls = 1
    assert await lease.release(session_status="idle") is True
    await _expire_claim(prepared.snapshot.effect_id)
    outcome = await effects.recover_effect_once(prepared.snapshot.effect_id)
    assert outcome == "manual_review"
    assert provider_calls == 1
    snapshot = await effects.get_effect(prepared.snapshot.effect_id)
    assert snapshot.state == "manual_review"
    assert snapshot.attempt_count == 1


@pytest.mark.asyncio
async def test_slow_provider_keeps_dispatch_claim_alive_until_receipt(monkeypatch):
    # SQLite's statement clock is second-resolution, so a two-second test TTL
    # still guarantees at least one full second before the first renewal.
    monkeypatch.setattr(effects, "EFFECT_LEASE_SECONDS", 2)
    _user, project, _session, lease, fence = await _seed("slow-provider")
    prepared = await _prepare(fence, project, adapter="slow-provider-adapter")
    claim = await effects.claim_effect_for_dispatch(
        prepared.snapshot.effect_id,
        fence,
    )
    await effects.mark_effect_submitting(claim)
    await effects.assert_effect_dispatchable(claim)

    async def slower_than_initial_claim():
        await asyncio.sleep(2.4)
        return {"provider_task": "completed"}

    response = await effects.run_with_effect_claim_heartbeat(
        claim,
        slower_than_initial_claim(),
    )
    assert response == {"provider_task": "completed"}
    accepted = await effects.record_effect_accepted(
        claim,
        provider_handle="slow-provider-task",
        receipt=response,
    )
    assert accepted.state == "accepted"
    assert accepted.provider_handle == "slow-provider-task"
    await effects.settle_effect(
        claim,
        state="manual_review",
        error={"code": "test_cleanup"},
    )
    await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_stale_agent_generation_cannot_publish_final_projection():
    _user, project, _session, lease, fence = await _seed("stale-projection")
    adapter = f"stale-projection-adapter-{uuid4().hex[:8]}"
    prepared = await _prepare(fence, project, adapter=adapter)
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, fence)
    await effects.mark_effect_submitting(claim)
    await effects.record_effect_accepted(
        claim,
        provider_handle="provider-task-stale",
        receipt={"task_id": "provider-task-stale"},
    )
    await lease.release(session_status="idle")
    with pytest.raises(driver.LeaseLostError):
        await effects.settle_effect(
            claim,
            state="succeeded",
            projection={"must_not_publish": True},
        )
    snapshot = await effects.get_effect(prepared.snapshot.effect_id)
    assert snapshot.state == "accepted"
    assert snapshot.projection is None
    await _expire_claim(prepared.snapshot.effect_id)
    assert await effects.recover_effect_once(prepared.snapshot.effect_id) == "manual_review"


@pytest.mark.asyncio
async def test_claim_takeover_fences_a_late_old_worker_receipt():
    _user, project, _session, lease, fence = await _seed("takeover")
    prepared = await _prepare(fence, project, adapter="takeover-adapter")
    old = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, fence)
    await effects.mark_effect_submitting(old)
    assert await lease.release(session_status="idle") is True
    await _expire_claim(prepared.snapshot.effect_id)

    replacement = await effects._claim_for_reconcile(prepared.snapshot.effect_id)
    assert replacement is not None
    assert replacement.generation == old.generation + 1
    with pytest.raises(effects.EffectLeaseLostError):
        await effects.record_effect_accepted(
            old,
            provider_handle="provider-task-late",
            receipt={"task_id": "provider-task-late"},
        )
    await effects.settle_effect(
        replacement,
        state="manual_review",
        error={"code": "receipt_arrived_after_takeover"},
    )


@pytest.mark.asyncio
async def test_receipt_and_projection_rollback_together_when_projector_fails():
    _user, project, _session, lease, fence = await _seed("atomic")
    prepared = await _prepare(fence, project, adapter="atomic-adapter")
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, fence)
    await effects.mark_effect_submitting(claim)
    await effects.record_effect_accepted(
        claim,
        provider_handle="task-atomic",
        receipt={"version": 1},
    )

    async def broken_projector(_db, row, _projection):
        row.safe_context = {"should": "roll back"}
        raise RuntimeError("injected projection failure")

    with pytest.raises(RuntimeError, match="injected"):
        await effects.settle_effect(
            claim,
            state="succeeded",
            receipt={"version": 2},
            projection={"asset_ids": ["asset-1"]},
            projector=broken_projector,
        )
    after_failure = await effects.get_effect(prepared.snapshot.effect_id)
    assert after_failure.state == "accepted"
    assert after_failure.provider_receipt == {"version": 1}
    assert after_failure.projection is None
    assert after_failure.safe_context == {"text": "Unicode 路径/你好😀"}
    phases = [item["phase"] for item in await effects.list_effect_evidence(prepared.snapshot.effect_id)]
    assert "succeeded" not in phases

    settled = await effects.settle_effect(
        claim,
        state="succeeded",
        receipt={"version": 2},
        projection={"asset_ids": ["asset-1"]},
    )
    assert settled.provider_receipt == {"version": 2}
    assert settled.projection == {"asset_ids": ["asset-1"]}
    await lease.release(session_status="idle")


def test_field_aware_secret_scrub_preserves_ordinary_unicode_text_exactly():
    ordinary = "示例 api_key=sk-not-a-real-key；路径/资料/你好😀.txt"
    safe = effects.sanitize_public_evidence({
        "text": ordinary,
        "api_key": "sk-real-secret",
        "Authorization": "Bearer secret",
        "nested": {
            "put_url": "https://oss.example.test/key?Signature=secret",
            "note": ordinary,
        },
    })
    assert safe["text"] == ordinary
    assert safe["nested"]["note"] == ordinary
    assert safe["api_key"] == "[redacted]"
    assert safe["Authorization"] == "[redacted]"
    assert safe["nested"]["put_url"] == "[redacted]"
    assert "secret" not in str(safe)


@pytest.mark.asyncio
async def test_bounded_restart_scanner_reconciles_only_the_requested_batch():
    _user, project, _session, lease, fence = await _seed("bounded")
    adapter = f"bounded-adapter-{uuid4().hex[:8]}"

    class Reconciler:
        can_reconcile_without_handle = True

        async def reconcile(self, effect):
            return effects.ReconcileDecision(
                state="succeeded",
                projection={"effect_id": effect.effect_id},
            )

    reconciler = Reconciler()
    effects.register_effect_reconciler(adapter, reconciler)
    effect_ids = []
    for index in range(2):
        prepared = await _prepare(
            fence,
            project,
            adapter=adapter,
            logical_key=f"call-{index}",
        )
        claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, fence)
        await effects.mark_effect_submitting(claim)
        await effects.record_effect_accepted(
            claim,
            provider_handle=None,
            receipt={"query_key": prepared.snapshot.idempotency_key},
        )
        effect_ids.append(prepared.snapshot.effect_id)
    await lease.release(session_status="idle")
    for effect_id in effect_ids:
        await _expire_claim(effect_id)

    first = await effects.recover_external_effects_once(limit=1)
    assert first.scanned == 1
    assert first.reconciled == 1
    states = [(await effects.get_effect(effect_id)).state for effect_id in effect_ids]
    assert states.count("succeeded") == 1
    second = await effects.recover_external_effects_once(limit=1)
    assert second.reconciled == 1
    final_states = [
        (await effects.get_effect(effect_id)).state for effect_id in effect_ids
    ]
    assert final_states == ["succeeded", "succeeded"]
    effects.unregister_effect_reconciler(adapter, reconciler)


@pytest.mark.asyncio
async def test_stale_prepared_intent_closes_as_definite_no_send():
    _user, project, _session, lease, fence = await _seed("prepared-recovery")
    prepared = await _prepare(fence, project, adapter="prepared-recovery-adapter")
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        await db.execute(
            update(ExternalEffect)
            .where(ExternalEffect.id == prepared.snapshot.effect_id)
            .values(
                prepared_at=datetime.now(timezone.utc)
                - timedelta(seconds=effects.PREPARED_RECOVERY_GRACE_SECONDS + 1)
            )
        )
    assert await effects.recover_effect_once(prepared.snapshot.effect_id) == "failed_before_dispatch"
    snapshot = await effects.get_effect(prepared.snapshot.effect_id)
    assert snapshot.state == "failed"
    phases = await effects.list_effect_evidence(prepared.snapshot.effect_id)
    assert phases[-1]["evidence"]["provider_called"] is False


def test_database_lease_expressions_use_server_clocks_for_sqlite_and_postgres():
    class Bind:
        def __init__(self, dialect):
            self.dialect = dialect

    class Db:
        def __init__(self, dialect):
            self._bind = Bind(dialect)

        def get_bind(self):
            return self._bind

    pg_expr = effects._database_expiry(Db(postgresql.dialect()))
    sqlite_expr = effects._database_expiry(Db(sqlite.dialect()))
    assert "clock_timestamp" in str(
        pg_expr.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "datetime" in str(
        sqlite_expr.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})
    )


def test_dispatch_cas_keeps_all_caller_identity_values_bound_on_sqlite_and_postgres():
    class Bind:
        def __init__(self, dialect):
            self.dialect = dialect

    class Db:
        def __init__(self, dialect):
            self._bind = Bind(dialect)

        def get_bind(self):
            return self._bind

    marker = "effect'; DROP TABLE external_effects; --"
    fence = effects.EffectRunFence(
        session_id="session' OR 1=1 --",
        tenant_id="tenant' OR 1=1 --",
        run_id="run' OR 1=1 --",
        generation=7,
    )
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        statement = effects._dispatch_claim_statement(
            Db(dialect),
            effect_id=marker,
            fence=fence,
            token="token' OR 1=1 --",
            owner_id="worker' OR 1=1 --",
        )
        compiled = statement.compile(dialect=dialect)
        sql = str(compiled)
        bound_values = set(compiled.params.values())
        assert marker not in sql
        assert fence.session_id not in sql
        assert fence.tenant_id not in sql
        assert fence.run_id not in sql
        assert marker in bound_values
        assert fence.session_id in bound_values
        assert fence.tenant_id in bound_values
        assert fence.run_id in bound_values
        assert "token' OR 1=1 --" in bound_values
        assert "worker' OR 1=1 --" in bound_values


@pytest.mark.asyncio
async def test_image_adapter_runs_prepare_receipt_and_projection_end_to_end(monkeypatch):
    import core.oss
    import tool.image_gen as image_mod
    from db.models.file_asset import FileAsset
    from tool.image_gen import ImageGenArgs, ProviderTarget, StoredImage
    from tool.tool import ToolContext

    user, project, session, lease, fence = await _seed("image-e2e")
    # A single OSS/materialization phase may exceed the normal effect TTL.
    # Keep this test short while proving that accepted effects are renewed
    # until their product projection commits.
    monkeypatch.setattr(effects, "EFFECT_LEASE_SECONDS", 2)
    settings = SimpleNamespace(
        default_size="auto",
        default_quality="medium",
        output_format="png",
        dedupe=False,
    )
    monkeypatch.setattr(
        image_mod,
        "_configured_target",
        lambda: (
            ProviderTarget(
                "openai", "gpt-image-2", "secret", "https://gateway.test/v1", 60
            ),
            settings,
        ),
    )
    monkeypatch.setattr(core.oss, "get_oss", lambda: object())
    monkeypatch.setattr(image_mod, "_load_inputs", _empty_inputs)
    provider_calls = 0

    async def provider(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return [b"\x89PNG\r\n\x1a\npaid-image"]

    async def store(_ctx, _oss, data, _fmt, _name, _prompt, _mode, _index, _total, *, reserved_asset):
        await asyncio.sleep(2.4)
        async with get_db_session() as db:
            await db.execute(
                update(FileAsset)
                .where(FileAsset.id == reserved_asset.id)
                .values(status="ready", size=len(data), mime="image/png")
            )
        return StoredImage(
            reserved_asset.id,
            reserved_asset.name,
            "image/png",
            len(data),
            f"/workspace/{reserved_asset.name}",
            attached=True,
            materialized=True,
        )

    monkeypatch.setattr(image_mod, "_call_provider", provider)
    monkeypatch.setattr(image_mod, "_store_output", store)
    ctx = ToolContext(
        user_id=user,
        project_id=project,
        session_id=session,
        message_id="message-image",
        part_id="part-image-stable",
        run_id=fence.run_id,
        run_generation=fence.generation,
        _assert_current=lease.assert_current,
    )
    result = await image_mod.execute(ImageGenArgs(prompt="画一个你好😀"), ctx)
    assert result.metadata["asset_ids"]
    assert provider_calls == 1
    async with get_db_session() as db:
        ledger = (
            await db.execute(
                select(ExternalEffect).where(
                    ExternalEffect.tenant_id == user,
                    ExternalEffect.adapter == "image_gen",
                )
            )
        ).scalar_one()
    assert ledger.state == "succeeded"
    assert ledger.attempt_count == 1
    assert ledger.projection["asset_ids"] == result.metadata["asset_ids"]
    phases = [item["phase"] for item in await effects.list_effect_evidence(ledger.id)]
    assert phases == ["prepared", "claim_acquired", "submitting", "accepted", "succeeded"]
    await lease.release(session_status="idle")


async def _empty_inputs(_refs, _mask, _ctx, _oss):
    return [], None
