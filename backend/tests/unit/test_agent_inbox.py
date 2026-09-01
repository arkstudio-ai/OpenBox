"""Durable main-Session inbox acceptance, claim and wake invariants."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import func, select, update

from agent import inbox
from agent.driver import (
    RecoveredDriver,
    get_driver_state,
    recover_expired_driver_records,
    reserve_recovered_run,
    reserve_run,
)
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_driver import AgentDriverState
from db.models.agent_inbox import AgentInboxItem
from db.models.file_asset import FileAsset
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from models.message import MessageInfo, MessageRole
from session.agent_event_log import (
    load_canonical_model_surface,
    project_agent_events,
    verify_agent_event_parity,
)
from session.event_range import freeze_fork_event_range
from session.fork import fork_session
from session.session import create_assistant_message, update_message_info


async def _seed(*, second_user: bool = False) -> tuple[str, str, str, str | None]:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"inbox-user-{suffix}"
    project_id = f"inbox-project-{suffix}"
    session_id = f"inbox-session-{suffix}"
    other_user_id = f"inbox-other-{suffix}" if second_user else None
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            User(
                id=user_id,
                username=user_id,
                created_at=now,
                updated_at=now,
            )
        )
        if other_user_id is not None:
            db.add(
                User(
                    id=other_user_id,
                    username=other_user_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        db.add(
            Project(
                id=project_id,
                user_id=user_id,
                name="Inbox",
                slug=f"inbox-{suffix}",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            Session(
                id=session_id,
                user_id=user_id,
                project_id=project_id,
                agent="build",
                model="test/model",
                status="idle",
                token_usage={},
                tool_exposure_state={},
                created_at=now,
                updated_at=now,
            )
        )
    return user_id, project_id, session_id, other_user_id


@pytest.mark.asyncio
async def test_accept_is_tenant_scoped_and_client_idempotent():
    user_id, _project_id, session_id, other_user = await _seed(second_user=True)
    first = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="first",
        client_id="stable-request",
        model="test/model",
    )
    replay = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="first",
        client_id="stable-request",
        model="test/model",
    )
    assert first.created is True
    assert replay.created is False
    assert replay.id == first.id
    with pytest.raises(inbox.InboxIdempotencyConflict):
        await inbox.accept_inbox_item(
            session_id=session_id,
            user_id=user_id,
            delivery="followup",
            prompt="changed",
            client_id="stable-request",
            model="test/model",
        )
    with pytest.raises(LookupError):
        await inbox.accept_inbox_item(
            session_id=session_id,
            user_id=other_user,
            delivery="followup",
            prompt="cross tenant",
        )
    assert await inbox.get_inbox_item(first.id, user_id=other_user) is None
    await inbox.cancel_inbox_items(
        session_id=session_id,
        user_id=user_id,
        item_ids=(first.id,),
        reason="test cleanup",
    )


@pytest.mark.asyncio
async def test_cross_user_attachment_is_rejected_before_acceptance():
    user_id, _project_id, session_id, other_user = await _seed(second_user=True)
    asset_id = f"foreign-asset-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            FileAsset(
                id=asset_id,
                user_id=other_user,
                name="foreign.txt",
                oss_key=f"assets/{asset_id}/foreign.txt",
                mime="text/plain",
                size=7,
                status="ready",
                source="user",
                transient=False,
                is_deleted=False,
                created_at=now,
            )
        )
    with pytest.raises(inbox.InboxAttachmentError):
        await inbox.accept_inbox_item(
            session_id=session_id,
            user_id=user_id,
            delivery="followup",
            prompt="do not attach",
            attachments=(asset_id,),
        )
    async with get_db_session() as db:
        assert (
            await db.execute(
                select(func.count(AgentInboxItem.id)).where(
                    AgentInboxItem.session_id == session_id,
                )
            )
        ).scalar_one() == 0


@pytest.mark.asyncio
async def test_claim_materializes_message_parts_event_and_trigger_atomically():
    user_id, project_id, session_id, _other = await _seed()
    now = datetime.now(timezone.utc)
    asset_id = f"asset-{uuid.uuid4().hex}"
    async with get_db_session() as db:
        db.add(
            FileAsset(
                id=asset_id,
                user_id=user_id,
                name="brief.pdf",
                oss_key=f"assets/{asset_id}/brief.pdf",
                mime="application/pdf",
                size=123,
                status="ready",
                source="user",
                transient=False,
                is_deleted=False,
                created_at=now,
            )
        )
    receipt = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="read this",
        attachments=(asset_id,),
        client_id="attachment-turn",
        model="test/model",
    )
    lease = await reserve_run(session_id, user_id)
    try:
        batch = await inbox.claim_inbox_boundary(
            lease,
            step=1,
            include_next_turn=True,
        )
        assert [item.id for item in batch.receipts] == [receipt.id]
        claimed = batch.receipts[0]
        assert claimed.state == "claimed"
        assert claimed.run_id == lease.run_id
        assert claimed.generation == lease.generation
        assert claimed.turn_id == claimed.message_id
        assert claimed.step_id.endswith(":1")
        state = await get_driver_state(session_id)
        assert state is not None
        assert state.trigger_message_id == claimed.message_id
        async with get_db_session() as db:
            message = (
                await db.execute(
                    select(Message).where(
                        Message.id == claimed.message_id,
                    )
                )
            ).scalar_one()
            parts = list(
                (
                    await db.execute(
                        select(Part)
                        .where(
                            Part.message_id == claimed.message_id,
                        )
                        .order_by(Part.created_at, Part.id)
                    )
                )
                .scalars()
                .all()
            )
            asset = (
                await db.execute(
                    select(FileAsset).where(
                        FileAsset.id == asset_id,
                    )
                )
            ).scalar_one()
            lifecycle = list(
                (
                    await db.execute(
                        select(AgentEvent)
                        .where(
                            AgentEvent.session_id == session_id,
                            AgentEvent.kind.in_(("inbox.accepted", "inbox.claimed")),
                        )
                        .order_by(AgentEvent.sequence)
                    )
                )
                .scalars()
                .all()
            )
        assert message.client_message_id == "attachment-turn"
        assert {part.type for part in parts} == {"text", "file"}
        assert asset.session_id == session_id
        assert asset.project_id == project_id
        assert [event.kind for event in lifecycle] == [
            "inbox.accepted",
            "inbox.claimed",
        ]
        assert lifecycle[-1].run_id == lease.run_id
        assert lifecycle[-1].generation == lease.generation
        assert lifecycle[-1].turn_id == claimed.turn_id
        assert lifecycle[-1].step_id == claimed.step_id
        await inbox.settle_claimed_inbox_items(
            lease,
            result_message_id=None,
            outcome="succeeded",
        )
    finally:
        await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_reserved_recovery_delivers_assets_from_every_claimed_boundary_item():
    user_id, _project_id, session_id, _other = await _seed()
    asset_ids = [f"recovery-asset-{uuid.uuid4().hex}" for _ in range(2)]
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        for ordinal, asset_id in enumerate(asset_ids):
            db.add(
                FileAsset(
                    id=asset_id,
                    user_id=user_id,
                    name=f"recovery-{ordinal}.txt",
                    oss_key=f"assets/{asset_id}/recovery-{ordinal}.txt",
                    mime="text/plain",
                    size=ordinal + 1,
                    status="ready",
                    source="user",
                    transient=False,
                    is_deleted=False,
                    created_at=now + timedelta(microseconds=ordinal),
                )
            )
    await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="steer",
        prompt="step attachment",
        attachments=(asset_ids[0],),
    )
    await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="turn attachment",
        attachments=(asset_ids[1],),
    )
    lease = await reserve_run(session_id, user_id)
    try:
        batch = await inbox.claim_inbox_boundary(
            lease,
            step=1,
            include_next_turn=True,
        )
        assert len(batch.receipts) == 2
        from agent.recovery import _trigger_state

        valid, answer_id, recovered_assets = await _trigger_state(
            RecoveredDriver(
                session_id=session_id,
                user_id=user_id,
                run_id=lease.run_id,
                generation=lease.generation,
                phase="reserved",
                trigger_message_id=batch.receipts[-1].turn_id,
            )
        )
        assert valid is True
        assert answer_id is None
        assert recovered_assets == asset_ids
        await inbox.settle_claimed_inbox_items(
            lease,
            result_message_id=None,
            outcome="succeeded",
        )
    finally:
        await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_inbox_lifecycle_events_are_surface_projector_noops():
    user_id, _project_id, session_id, _other = await _seed()
    accepted = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="event parity",
    )
    lease = await reserve_run(session_id, user_id)
    try:
        batch = await inbox.claim_inbox_boundary(
            lease,
            step=1,
            include_next_turn=True,
        )
        trigger_id = batch.receipts[-1].message_id
        assistant = await create_assistant_message(
            session_id,
            trigger_id,
            model_id="test/model",
            agent="build",
            user_id=user_id,
            run_fence=(session_id, lease.run_id, lease.generation),
        )
        await update_message_info(
            MessageInfo(
                id=assistant.id,
                sessionID=session_id,
                role=MessageRole.ASSISTANT,
                parent_id=trigger_id,
                model_id="test/model",
                agent="build",
                finish="stop",
            ),
            user_id=user_id,
            run_fence=(session_id, lease.run_id, lease.generation),
        )
        assert await inbox.settle_claimed_inbox_items(
            lease,
            result_message_id=assistant.id,
            outcome="succeeded",
        ) == (accepted.id,)
        report = await verify_agent_event_parity(
            session_id,
            user_id=user_id,
            require_closed=True,
        )
        assert report.ok is True
        async with get_db_session() as db:
            lifecycle = list(
                (
                    await db.execute(
                        select(AgentEvent.kind)
                        .where(
                            AgentEvent.session_id == session_id,
                            AgentEvent.kind.like("inbox.%"),
                        )
                        .order_by(AgentEvent.sequence)
                    )
                )
                .scalars()
                .all()
            )
        assert lifecycle == ["inbox.accepted", "inbox.claimed", "inbox.settled"]
    finally:
        await lease.release(session_status="idle")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["materialized", "bound"])
async def test_claim_failpoint_rolls_back_transcript_and_binding(stage):
    user_id, _project_id, session_id, _other = await _seed()
    receipt = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="survive claim crash",
        client_id=f"fault-{stage}",
    )
    lease = await reserve_run(session_id, user_id)

    def failpoint(point: str) -> None:
        if point == stage:
            raise RuntimeError(stage)

    try:
        with pytest.raises(RuntimeError, match=stage):
            await inbox.claim_inbox_boundary(
                lease,
                step=1,
                include_next_turn=True,
                fault=failpoint,
            )
        current = await inbox.get_inbox_item(receipt.id, user_id=user_id)
        assert current is not None
        assert current.state == "accepted"
        assert current.message_id is None
        state = await get_driver_state(session_id)
        assert state is not None
        assert state.trigger_message_id is None
        async with get_db_session() as db:
            assert (
                await db.execute(
                    select(func.count(Message.id)).where(
                        Message.session_id == session_id,
                    )
                )
            ).scalar_one() == 0
            assert (
                await db.execute(
                    select(func.count(AgentEvent.id)).where(
                        AgentEvent.session_id == session_id,
                        AgentEvent.kind == "inbox.claimed",
                    )
                )
            ).scalar_one() == 0
    finally:
        await inbox.cancel_inbox_items(
            session_id=session_id,
            user_id=user_id,
            item_ids=(receipt.id,),
            reason="test cleanup",
        )
        await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_boundary_fifo_drains_step_before_one_turn():
    user_id, _project_id, session_id, _other = await _seed()
    follow_one = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="f1",
    )
    steer_one = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="steer",
        prompt="s1",
    )
    steer_two = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="inject",
        prompt="s2",
    )
    follow_two = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="f2",
    )
    lease = await reserve_run(session_id, user_id)
    try:
        batch = await inbox.claim_inbox_boundary(
            lease,
            step=1,
            include_next_turn=True,
        )
        assert [item.id for item in batch.receipts] == [
            steer_one.id,
            steer_two.id,
            follow_one.id,
        ]
        pending = await inbox.get_inbox_item(follow_two.id, user_id=user_id)
        assert pending is not None and pending.state == "accepted"
        late_steer = await inbox.accept_inbox_item(
            session_id=session_id,
            user_id=user_id,
            delivery="steer",
            prompt="late step",
        )
        second_boundary = await inbox.claim_inbox_boundary(
            lease,
            step=2,
            include_next_turn=False,
        )
        assert [item.id for item in second_boundary.receipts] == [late_steer.id]
        assert second_boundary.receipts[0].turn_id == batch.receipts[0].turn_id
        assert second_boundary.receipts[0].step_id.endswith(":2")
        await inbox.settle_claimed_inbox_items(
            lease,
            result_message_id=None,
            outcome="succeeded",
        )
        await inbox.cancel_inbox_items(
            session_id=session_id,
            user_id=user_id,
            item_ids=(follow_two.id,),
            reason="test cleanup",
        )
    finally:
        await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_busy_followup_queues_without_preempting_current_generation():
    user_id, _project_id, session_id, _other = await _seed()
    active = await reserve_run(session_id, user_id, run_id="active-run")
    try:
        receipt = await inbox.accept_inbox_item(
            session_id=session_id,
            user_id=user_id,
            delivery="followup",
            prompt="wait your turn",
        )
        assert await inbox.wake_inbox_session(session_id, user_id) is None
        state = await get_driver_state(session_id)
        assert state is not None
        assert state.run_id == active.run_id
        assert state.generation == active.generation
        assert state.abort_requested_at is None
        assert not active.abort.is_set()
        current = await inbox.get_inbox_item(receipt.id, user_id=user_id)
        assert current is not None and current.state == "accepted"
    finally:
        await inbox.cancel_inbox_items(
            session_id=session_id,
            user_id=user_id,
            item_ids=(receipt.id,),
            reason="test cleanup",
        )
        await active.release(session_status="idle")


@pytest.mark.asyncio
async def test_step_only_wake_does_not_absorb_a_late_busy_followup():
    user_id, _project_id, session_id, _other = await _seed()
    steer = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="steer",
        prompt="start this step",
    )
    lease = await reserve_run(session_id, user_id)
    followup = None
    try:
        first = await inbox.claim_inbox_boundary(
            lease,
            step=1,
            include_next_turn=True,
        )
        assert [item.id for item in first.receipts] == [steer.id]
        assert await inbox.run_has_claimed_turn(lease) is True

        followup = await inbox.accept_inbox_item(
            session_id=session_id,
            user_id=user_id,
            delivery="followup",
            prompt="wait for the next turn",
        )
        second = await inbox.claim_inbox_boundary(
            lease,
            step=2,
            include_next_turn=not await inbox.run_has_claimed_turn(lease),
        )
        assert second.empty
        pending = await inbox.get_inbox_item(followup.id, user_id=user_id)
        assert pending is not None and pending.state == "accepted"
        await inbox.settle_claimed_inbox_items(
            lease,
            result_message_id=None,
            outcome="succeeded",
        )
    finally:
        if followup is not None:
            await inbox.cancel_inbox_items(
                session_id=session_id,
                user_id=user_id,
                item_ids=(followup.id,),
                reason="test cleanup",
            )
        await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_explicit_regenerate_trigger_on_old_inbox_message_owns_its_turn():
    user_id, _project_id, session_id, _other = await _seed()
    await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="original inbox prompt",
    )
    original = await reserve_run(session_id, user_id)
    try:
        batch = await inbox.claim_inbox_boundary(
            original,
            step=1,
            include_next_turn=True,
        )
        historical_message_id = batch.receipts[0].message_id
        await inbox.settle_claimed_inbox_items(
            original,
            result_message_id=None,
            outcome="succeeded",
        )
    finally:
        await original.release(session_status="idle")

    assert historical_message_id is not None
    replacement = await reserve_run(session_id, user_id)
    try:
        await replacement.bind_trigger_message(historical_message_id)
        assert await inbox.run_has_claimed_turn(replacement) is True
    finally:
        await replacement.release(session_status="idle")


@pytest.mark.asyncio
async def test_inject_does_not_wake_and_cancel_is_terminal():
    user_id, _project_id, session_id, _other = await _seed()
    receipt = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="inject",
        prompt="context only",
    )
    assert await inbox.wake_inbox_session(session_id, user_id) is None
    assert await get_driver_state(session_id) is None
    assert await inbox.cancel_inbox_items(
        session_id=session_id,
        user_id=user_id,
        item_ids=(receipt.id,),
        reason="test cancel",
    ) == (receipt.id,)
    terminal = await inbox.wait_for_inbox_terminal(
        receipt.id,
        user_id=user_id,
        timeout=1,
    )
    assert terminal.state == "canceled"
    assert terminal.outcome == "canceled"


@pytest.mark.asyncio
async def test_concurrent_wake_scanners_reserve_only_one_driver(monkeypatch):
    user_id, _project_id, session_id, _other = await _seed()
    receipt = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="steer",
        prompt="wake once",
    )
    started = asyncio.Event()
    finish = asyncio.Event()
    driven = []

    async def fake_drive(lease, batch):
        driven.append((lease, batch))
        started.set()
        await finish.wait()
        await inbox.settle_claimed_inbox_items(
            lease,
            result_message_id=None,
            outcome="succeeded",
        )
        await lease.release(session_status="idle")

    monkeypatch.setattr(inbox, "_drive_claimed", fake_drive)
    run_ids = await asyncio.gather(
        *(inbox.wake_inbox_session(session_id, user_id) for _ in range(12))
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert sum(run_id is not None for run_id in run_ids) == 1
    assert len(driven) == 1
    claimed = await inbox.get_inbox_item(receipt.id, user_id=user_id)
    assert claimed is not None and claimed.state == "claimed"
    finish.set()
    await inbox.quiesce_inbox_tasks(timeout=1)
    terminal = await inbox.get_inbox_item(receipt.id, user_id=user_id)
    assert terminal is not None and terminal.state == "settled"


@pytest.mark.asyncio
async def test_expired_reserved_takeover_rebinds_exact_claim_without_replay():
    user_id, _project_id, session_id, _other = await _seed()
    accepted = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="survive takeover",
    )
    first = await reserve_run(session_id, user_id)
    batch = await inbox.claim_inbox_boundary(
        first,
        step=1,
        include_next_turn=True,
    )
    original_message_id = batch.receipts[0].message_id
    await first.stop_monitor()
    async with get_db_session() as db:
        await db.execute(
            update(AgentDriverState)
            .where(
                AgentDriverState.session_id == session_id,
            )
            .values(
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
    records = [
        record
        for record in await recover_expired_driver_records()
        if record.session_id == session_id
    ]
    assert len(records) == 1
    takeover = await reserve_recovered_run(records[0], initial_phase="reserved")
    try:
        assert await inbox.rebind_recovered_claims(records[0], takeover) == 1
        rebound = await inbox.get_inbox_item(accepted.id, user_id=user_id)
        assert rebound is not None
        assert rebound.state == "claimed"
        assert rebound.message_id == original_message_id
        assert rebound.run_id == takeover.run_id
        assert rebound.generation == takeover.generation
        async with get_db_session() as db:
            assert (
                await db.execute(
                    select(func.count(Message.id)).where(
                        Message.session_id == session_id,
                        Message.role == "user",
                    )
                )
            ).scalar_one() == 1
        await inbox.settle_claimed_inbox_items(
            takeover,
            result_message_id=None,
            outcome="recovered",
        )
    finally:
        await takeover.release(session_status="idle")
        await first.release(session_status="error")


@pytest.mark.asyncio
async def test_expired_claim_scanner_settles_exact_terminal_after_release_crash():
    user_id, _project_id, session_id, _other = await _seed()
    accepted = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="terminal settle crash",
    )
    lease = await reserve_run(session_id, user_id)
    batch = await inbox.claim_inbox_boundary(
        lease,
        step=1,
        include_next_turn=True,
    )
    trigger_id = batch.receipts[-1].message_id
    assistant = await create_assistant_message(
        session_id,
        trigger_id,
        model_id="test/model",
        agent="build",
        user_id=user_id,
        run_fence=(session_id, lease.run_id, lease.generation),
    )
    await update_message_info(
        MessageInfo(
            id=assistant.id,
            sessionID=session_id,
            role=MessageRole.ASSISTANT,
            parent_id=trigger_id,
            model_id="test/model",
            agent="build",
            finish="stop",
        ),
        user_id=user_id,
        run_fence=(session_id, lease.run_id, lease.generation),
    )
    assert await lease.release(session_status="idle") is True
    async with get_db_session() as db:
        await db.execute(
            update(AgentInboxItem)
            .where(
                AgentInboxItem.id == accepted.id,
            )
            .values(
                claim_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )

    assert await inbox.settle_orphaned_claims() == 1
    terminal = await inbox.get_inbox_item(accepted.id, user_id=user_id)
    assert terminal is not None
    assert terminal.state == "settled"
    assert terminal.outcome == "recovered"
    assert terminal.result_message_id == assistant.id


@pytest.mark.asyncio
async def test_invalid_attachment_cancels_only_its_item_and_same_reservation_continues():
    user_id, _project_id, session_id, _other = await _seed()
    now = datetime.now(timezone.utc)
    bad_asset_id = f"bad-boundary-{uuid.uuid4().hex}"
    good_asset_id = f"good-boundary-{uuid.uuid4().hex}"
    async with get_db_session() as db:
        for asset_id in (bad_asset_id, good_asset_id):
            db.add(
                FileAsset(
                    id=asset_id,
                    user_id=user_id,
                    name=f"{asset_id}.txt",
                    oss_key=f"assets/{asset_id}.txt",
                    mime="text/plain",
                    size=1,
                    status="ready",
                    source="user",
                    transient=False,
                    is_deleted=False,
                    created_at=now,
                )
            )
    bad = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="steer",
        prompt="this attachment disappears",
        attachments=(bad_asset_id,),
    )
    good = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="this valid item must still run",
        attachments=(good_asset_id,),
    )
    async with get_db_session() as db:
        await db.execute(
            update(FileAsset)
            .where(
                FileAsset.id == bad_asset_id,
            )
            .values(is_deleted=True, deleted_at=now)
        )

    claimed = await inbox._reserve_and_claim(session_id, user_id)
    assert claimed is not None
    lease, batch = claimed
    try:
        assert [receipt.id for receipt in batch.receipts] == [good.id]
        canceled = await inbox.get_inbox_item(bad.id, user_id=user_id)
        assert canceled is not None
        assert canceled.state == "canceled"
        assert "missing" in (canceled.error or {}).get("message", "")
        current = await inbox.get_inbox_item(good.id, user_id=user_id)
        assert current is not None
        assert current.state == "claimed"
        assert current.run_id == lease.run_id
        await inbox.settle_claimed_inbox_items(
            lease,
            result_message_id=None,
            outcome="succeeded",
        )
    finally:
        await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_strict_delivery_failure_preserves_claim_for_recovery(monkeypatch):
    user_id, _project_id, session_id, _other = await _seed()
    now = datetime.now(timezone.utc)
    asset_id = f"strict-fail-{uuid.uuid4().hex}"
    async with get_db_session() as db:
        db.add(
            FileAsset(
                id=asset_id,
                user_id=user_id,
                name="strict.txt",
                oss_key=f"assets/{asset_id}/strict.txt",
                mime="text/plain",
                size=6,
                status="ready",
                source="user",
                transient=False,
                is_deleted=False,
                created_at=now,
            )
        )
    accepted = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="must have the file",
        attachments=(asset_id,),
    )
    claimed = await inbox._reserve_and_claim(session_id, user_id)
    assert claimed is not None
    lease, batch = claimed
    calls: list[tuple[list[str], dict]] = []
    ran: list[bool] = []

    async def fail_delivery(_session_id, _user_id, asset_ids, **kwargs):
        calls.append((list(asset_ids), kwargs))
        from sandbox.assets import AssetDeliveryError

        raise AssetDeliveryError(
            expected_asset_ids=asset_ids,
            missing_asset_ids=asset_ids,
        )

    async def should_not_run(*_args, **_kwargs):
        ran.append(True)

    import agent.loop as loop_module
    import sandbox.assets as assets_module

    monkeypatch.setattr(assets_module, "deliver_asset_ids", fail_delivery)
    monkeypatch.setattr(loop_module, "run_loop", should_not_run)
    await inbox._drive_claimed(lease, batch)

    assert ran == []
    assert calls == [
        (
            [asset_id],
            {
                "strict": True,
                "expected_asset_ids": [asset_id],
            },
        )
    ]
    current = await inbox.get_inbox_item(accepted.id, user_id=user_id)
    assert current is not None and current.state == "claimed"
    state = await get_driver_state(session_id)
    assert state is not None
    assert state.phase == "reserved"
    assert state.run_id == lease.run_id
    expiry = state.lease_expires_at
    assert expiry is not None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    assert expiry <= datetime.now(timezone.utc)
    async with get_db_session() as db:
        session_status = (
            await db.execute(
                select(Session.status).where(
                    Session.id == session_id,
                )
            )
        ).scalar_one()
    assert session_status == "error"

    record = RecoveredDriver(
        session_id=session_id,
        user_id=user_id,
        run_id=state.run_id,
        generation=state.generation,
        phase=state.phase,
        trigger_message_id=state.trigger_message_id,
    )
    cleanup = await reserve_recovered_run(record, initial_phase="finalizing")
    await inbox.rebind_recovered_claims(record, cleanup)
    await inbox.settle_claimed_inbox_items(
        cleanup,
        result_message_id=None,
        outcome="delivery_error",
        error={"message": "test cleanup"},
    )
    await cleanup.release(session_status="idle")


@pytest.mark.asyncio
async def test_deleted_after_claim_settles_delivery_error_without_provider(monkeypatch):
    user_id, _project_id, session_id, _other = await _seed()
    asset_id = f"deleted-after-claim-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            FileAsset(
                id=asset_id,
                user_id=user_id,
                name="deleted.txt",
                oss_key=f"assets/{asset_id}/deleted.txt",
                mime="text/plain",
                size=7,
                status="ready",
                source="user",
                transient=False,
                is_deleted=False,
                created_at=now,
            )
        )
    accepted = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="must not run without the deleted file",
        attachments=(asset_id,),
    )
    claimed = await inbox._reserve_and_claim(session_id, user_id)
    assert claimed is not None
    lease, batch = claimed
    async with get_db_session() as db:
        await db.execute(
            update(FileAsset)
            .where(
                FileAsset.id == asset_id,
            )
            .values(is_deleted=True)
        )

    provider_calls: list[bool] = []

    async def should_not_run(*_args, **_kwargs):
        provider_calls.append(True)

    import agent.loop as loop_module

    monkeypatch.setattr(loop_module, "run_loop", should_not_run)
    await inbox._drive_claimed(lease, batch)

    assert provider_calls == []
    terminal = await inbox.get_inbox_item(accepted.id, user_id=user_id)
    assert terminal is not None
    assert terminal.state == "settled"
    assert terminal.outcome == "delivery_error"
    assert terminal.delivery_attempts == 1
    assert terminal.delivery_last_error is not None
    assert terminal.delivery_last_error["code"] == "asset_unavailable"
    assert terminal.delivery_last_error["retryable"] is False
    assert terminal.result_message_id is not None
    async with get_db_session() as db:
        assistant = (
            await db.execute(
                select(Message).where(
                    Message.id == terminal.result_message_id,
                )
            )
        ).scalar_one()
        status = (
            await db.execute(
                select(Session.status).where(
                    Session.id == session_id,
                )
            )
        ).scalar_one()
    assert assistant.finish == "error"
    assert assistant.error == inbox.DELIVERY_TERMINAL_ERROR
    assert status == "error"
    state = await get_driver_state(session_id)
    assert state is not None and state.phase == "idle"
    parity = await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )
    assert parity.ok is True
    assert not any(
        record.session_id == session_id
        for record in await recover_expired_driver_records()
    )


@pytest.mark.asyncio
async def test_transient_delivery_attempt_survives_restart_then_succeeds(monkeypatch):
    user_id, _project_id, session_id, _other = await _seed()
    asset_id = f"retry-success-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            FileAsset(
                id=asset_id,
                user_id=user_id,
                name="eventual.txt",
                oss_key=f"assets/{asset_id}/eventual.txt",
                mime="text/plain",
                size=8,
                status="ready",
                source="user",
                transient=False,
                is_deleted=False,
                created_at=now,
            )
        )
    accepted = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="retry the transfer",
        attachments=(asset_id,),
    )
    claimed = await inbox._reserve_and_claim(session_id, user_id)
    assert claimed is not None
    lease, batch = claimed
    delivery_calls: list[str] = []

    async def flaky_delivery(_session_id, _user_id, asset_ids, **_kwargs):
        delivery_calls.append(asset_ids[0])
        if len(delivery_calls) == 1:
            from sandbox.assets import AssetDeliveryError

            raise AssetDeliveryError(
                expected_asset_ids=asset_ids,
                missing_asset_ids=asset_ids,
                retryable=True,
            )
        return [f"/workspace/{asset_ids[0]}"]

    provider_calls: list[int] = []

    async def fake_run_loop(_session_id, user_id, *, lease):
        provider_calls.append(lease.generation)
        current = await inbox.get_inbox_item(accepted.id, user_id=user_id)
        assert current is not None
        assert current.delivery_attempts == 1
        assert current.delivery_last_error is None
        await inbox.settle_claimed_inbox_items(
            lease,
            result_message_id=None,
            outcome="succeeded",
        )
        await lease.release(session_status="idle")

    import agent.loop as loop_module
    import agent.recovery as recovery_module
    import sandbox.assets as assets_module

    monkeypatch.setattr(assets_module, "deliver_asset_ids", flaky_delivery)
    monkeypatch.setattr(loop_module, "run_loop", fake_run_loop)
    await inbox._drive_claimed(lease, batch)
    first = await inbox.get_inbox_item(accepted.id, user_id=user_id)
    assert first is not None
    assert first.state == "claimed"
    assert first.delivery_attempts == 1

    record = next(
        record
        for record in await recover_expired_driver_records()
        if record.session_id == session_id
    )
    resumed, invalid = await recovery_module.resume_reserved_prompts([record])
    assert resumed == [session_id]
    assert invalid == []
    tasks = list(recovery_module._resume_tasks)
    if tasks:
        await asyncio.gather(*tasks)

    terminal = await inbox.get_inbox_item(accepted.id, user_id=user_id)
    assert terminal is not None
    assert terminal.state == "settled"
    assert terminal.outcome == "succeeded"
    assert terminal.delivery_attempts == 1
    assert terminal.delivery_last_error is None
    assert delivery_calls == [asset_id, asset_id]
    assert provider_calls == [2]


@pytest.mark.asyncio
async def test_repeated_restart_delivery_failure_reaches_terminal_once(monkeypatch):
    user_id, _project_id, session_id, _other = await _seed()
    asset_id = f"retry-terminal-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            FileAsset(
                id=asset_id,
                user_id=user_id,
                name="never.txt",
                oss_key=f"assets/{asset_id}/never.txt",
                mime="text/plain",
                size=5,
                status="ready",
                source="user",
                transient=False,
                is_deleted=False,
                created_at=now,
            )
        )
    accepted = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="bounded retries",
        attachments=(asset_id,),
    )
    claimed = await inbox._reserve_and_claim(session_id, user_id)
    assert claimed is not None
    lease, batch = claimed
    delivery_calls: list[int] = []
    provider_calls: list[bool] = []

    async def always_fail(_session_id, _user_id, asset_ids, **_kwargs):
        delivery_calls.append(1)
        from sandbox.assets import AssetDeliveryError

        raise AssetDeliveryError(
            expected_asset_ids=asset_ids,
            missing_asset_ids=asset_ids,
            retryable=True,
        )

    async def should_not_run(*_args, **_kwargs):
        provider_calls.append(True)

    import agent.loop as loop_module
    import agent.recovery as recovery_module
    import sandbox.assets as assets_module

    monkeypatch.setattr(assets_module, "deliver_asset_ids", always_fail)
    monkeypatch.setattr(loop_module, "run_loop", should_not_run)
    await inbox._drive_claimed(lease, batch)

    for expected_attempt in (2, 3):
        record = next(
            record
            for record in await recover_expired_driver_records()
            if record.session_id == session_id
        )
        resumed, invalid = await recovery_module.resume_reserved_prompts([record])
        assert resumed == [session_id]
        assert invalid == []
        tasks = list(recovery_module._resume_tasks)
        if tasks:
            await asyncio.gather(*tasks)
        current = await inbox.get_inbox_item(accepted.id, user_id=user_id)
        assert current is not None
        assert current.delivery_attempts == expected_attempt

    terminal = await inbox.get_inbox_item(accepted.id, user_id=user_id)
    assert terminal is not None
    assert terminal.state == "settled"
    assert terminal.outcome == "delivery_error"
    assert terminal.result_message_id is not None
    assert terminal.delivery_attempts == inbox.MAX_DURABLE_DELIVERY_ATTEMPTS
    assert delivery_calls == [1, 1, 1]
    assert provider_calls == []
    state = await get_driver_state(session_id)
    assert state is not None and state.phase == "idle"
    assert not any(
        record.session_id == session_id
        for record in await recover_expired_driver_records()
    )


@pytest.mark.asyncio
async def test_mixed_delivery_failure_settles_only_bad_item(monkeypatch):
    user_id, _project_id, session_id, _other = await _seed()
    now = datetime.now(timezone.utc)
    bad_asset = f"mixed-bad-{uuid.uuid4().hex}"
    good_asset = f"mixed-good-{uuid.uuid4().hex}"
    async with get_db_session() as db:
        for asset_id in (bad_asset, good_asset):
            db.add(
                FileAsset(
                    id=asset_id,
                    user_id=user_id,
                    name=f"{asset_id}.txt",
                    oss_key=f"assets/{asset_id}/{asset_id}.txt",
                    mime="text/plain",
                    size=4,
                    status="ready",
                    source="user",
                    transient=False,
                    is_deleted=False,
                    created_at=now,
                )
            )
    good = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="steer",
        prompt="good attachment",
        attachments=(good_asset,),
    )
    # Put the terminally bad item last. Without model-only exclusion this is
    # the dangerous ordering: an Assistant answering the good item leaves the
    # failed User/FilePart as the apparent latest prompt.
    bad = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="steer",
        prompt="bad attachment",
        attachments=(bad_asset,),
    )
    claimed = await inbox._reserve_and_claim(session_id, user_id)
    assert claimed is not None
    lease, batch = claimed
    receipt_by_item = {receipt.id: receipt for receipt in batch.receipts}
    good_message_id = receipt_by_item[good.id].message_id
    bad_message_id = receipt_by_item[bad.id].message_id
    provider_calls: list[int] = []
    assistant_ids: list[str] = []

    async def per_item_delivery(_session_id, _user_id, asset_ids, **_kwargs):
        if asset_ids == [bad_asset]:
            from sandbox.assets import AssetDeliveryError

            raise AssetDeliveryError(
                expected_asset_ids=asset_ids,
                missing_asset_ids=asset_ids,
                code="asset_unavailable",
                retryable=False,
            )
        assert asset_ids == [good_asset]
        return [f"/workspace/{good_asset}"]

    async def fake_run_loop(_session_id, user_id, *, lease):
        provider_calls.append(lease.generation)
        surface = await load_canonical_model_surface(
            _session_id,
            user_id=user_id,
            run_fence=(_session_id, lease.run_id, lease.generation),
            repair_tail=False,
        )
        visible_ids = {message.id for message in surface.messages}
        assert good_message_id in visible_ids
        assert bad_message_id not in visible_ids
        assistant = await create_assistant_message(
            _session_id,
            good_message_id,
            model_id="test/model",
            agent="build",
            user_id=user_id,
            run_fence=(_session_id, lease.run_id, lease.generation),
        )
        await update_message_info(
            MessageInfo(
                id=assistant.id,
                sessionID=_session_id,
                role=MessageRole.ASSISTANT,
                parent_id=good_message_id,
                model_id="test/model",
                agent="build",
                finish="stop",
            ),
            user_id=user_id,
            run_fence=(_session_id, lease.run_id, lease.generation),
        )
        assistant_ids.append(assistant.id)
        await inbox.settle_claimed_inbox_items(
            lease,
            result_message_id=assistant.id,
            outcome="succeeded",
        )
        await lease.release(session_status="idle")

    import agent.loop as loop_module
    import sandbox.assets as assets_module

    monkeypatch.setattr(assets_module, "deliver_asset_ids", per_item_delivery)
    monkeypatch.setattr(loop_module, "run_loop", fake_run_loop)
    await inbox._drive_claimed(lease, batch)

    bad_terminal = await inbox.get_inbox_item(bad.id, user_id=user_id)
    good_terminal = await inbox.get_inbox_item(good.id, user_id=user_id)
    assert bad_terminal is not None and good_terminal is not None
    assert bad_terminal.state == "settled"
    assert bad_terminal.outcome == "delivery_error"
    assert bad_terminal.delivery_attempts == 1
    assert good_terminal.state == "settled"
    assert good_terminal.outcome == "succeeded"
    assert good_terminal.delivery_attempts == 0
    assert provider_calls == [1]
    assert len(assistant_ids) == 1

    report = await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )
    assert report.ok is True
    async with get_db_session() as db:
        source_events = list((await db.execute(select(AgentEvent).where(
            AgentEvent.session_id == session_id,
            AgentEvent.user_id == user_id,
        ).order_by(AgentEvent.sequence))).scalars().all())
    public = project_agent_events(source_events)
    public_ids = {str(message["id"]) for message in public["messages"]}
    assert {good_message_id, bad_message_id}.issubset(public_ids)

    frozen = await freeze_fork_event_range(
        session_id,
        user_id=user_id,
        up_to_message_id=assistant_ids[0],
    )
    assert {good_message_id, bad_message_id}.issubset(
        set(frozen.covered_message_ids)
    )
    child = await fork_session(
        session_id,
        up_to_message_id=assistant_ids[0],
        user_id=user_id,
    )
    child_model = await load_canonical_model_surface(
        child.id,
        user_id=user_id,
        repair_tail=False,
    )
    child_model_texts = {
        part.text
        for message in child_model.messages
        for part in message.parts
        if getattr(part, "type", None) == "text"
    }
    assert "good attachment" in child_model_texts
    assert "bad attachment" not in child_model_texts
    async with get_db_session() as db:
        child_events = list((await db.execute(select(AgentEvent).where(
            AgentEvent.session_id == child.id,
            AgentEvent.user_id == user_id,
        ).order_by(AgentEvent.sequence))).scalars().all())
    child_public = project_agent_events(child_events)
    child_public_texts = {
        str(part.get("data", {}).get("text"))
        for message in child_public["messages"]
        for part in message.get("parts") or []
        if part.get("type") == "text"
    }
    assert {"good attachment", "bad attachment"}.issubset(child_public_texts)


@pytest.mark.asyncio
async def test_item_waiters_do_not_grow_with_historical_prompts():
    user_id, _project_id, session_id, _other = await _seed()
    accepted = await inbox.accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="inject",
        prompt="wait briefly",
    )
    before = set(inbox._item_events)
    inbox._notify(("never-waited-history",))
    assert set(inbox._item_events) == before
    with pytest.raises(TimeoutError):
        await inbox.wait_for_inbox_terminal(
            accepted.id,
            user_id=user_id,
            timeout=0.01,
        )
    assert accepted.id not in inbox._item_events
    await inbox.cancel_inbox_items(
        session_id=session_id,
        user_id=user_id,
        item_ids=(accepted.id,),
        reason="test cleanup",
    )
    assert accepted.id not in inbox._item_events
