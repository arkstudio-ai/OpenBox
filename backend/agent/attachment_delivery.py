"""Bounded attachment recovery for original turns without a new Inbox claim.

An explicit resume retains the original settled inputs. Its delivery budget
belongs to the original resume Command, across recovery generations, without
rewriting those inputs or manufacturing another user request.
"""
from sqlalchemy import JSON, select, type_coerce

from agent import inbox
from agent.effect_ledger import request_hash
from assistant.control import resume_binding_locked
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from session.agent_event_log import append_agent_event_locked, prepare_agent_event_write


async def _context_locked(db, lease):
    fence = (lease.session_id, lease.run_id, lease.generation)
    session = await prepare_agent_event_write(db, session_id=lease.session_id,
        user_id=lease.user_id, run_fence=fence)
    driver = await db.get(AgentDriverState, lease.session_id)
    trigger = driver.trigger_message_id
    if not trigger:
        raise inbox.InboxError("attachment continuation has no original trigger")
    binding = await resume_binding_locked(db, lease.session_id, lease.run_id, lease.generation)
    if binding and binding.payload.get("trigger_message_id") != trigger:
        raise inbox.InboxError("attachment continuation changed its original trigger")
    origin = {"trigger_message_id": trigger}
    if binding:
        origin["resume_command_id"] = binding.payload["command_id"]
    key = request_hash(origin)
    previous = await db.scalar(select(AgentEvent).where(
        AgentEvent.session_id == lease.session_id, AgentEvent.user_id == lease.user_id,
        AgentEvent.kind == "attachment.delivery_failed", AgentEvent.turn_id == trigger,
        type_coerce(AgentEvent.payload, JSON)["delivery_key"].as_string() == key,
    ).order_by(AgentEvent.sequence.desc()).limit(1))
    originals = list((await db.scalars(select(AgentInboxItem).where(
        AgentInboxItem.session_id == lease.session_id, AgentInboxItem.user_id == lease.user_id,
        AgentInboxItem.turn_id == trigger, AgentInboxItem.state.in_(("claimed", "settled")),
    ).order_by(AgentInboxItem.created_at, AgentInboxItem.id))).all()) if binding else []
    parent = await inbox.latest_turn_input_locked(db, session_id=lease.session_id,
        user_id=lease.user_id, trigger_message_id=trigger)
    if parent is None:
        raise inbox.InboxError("attachment continuation lost its original input")
    return session, origin, key, previous, originals, parent


def _terminal(previous):
    message_id = previous.payload.get("result_message_id") if previous else None
    return inbox.AttachmentDeliveryResult((), (), result_message_id=message_id) if message_id else None


async def _record_failure(lease, expected, error):
    result = None
    message = None
    fence = (lease.session_id, lease.run_id, lease.generation)
    async with get_db_session() as db:
        session, origin, key, previous, _, parent = await _context_locked(db, lease)
        terminal = _terminal(previous)
        if terminal is not None:
            return terminal
        attempt = min(int(previous.payload["attempt"]) + 1 if previous else 1,
            inbox.MAX_DURABLE_DELIVERY_ATTEMPTS)
        terminal = not error["retryable"] or attempt >= inbox.MAX_DURABLE_DELIVERY_ATTEMPTS
        now = await inbox._database_utcnow(db)
        if terminal:
            message = await inbox.create_delivery_error_locked(db, session, lease=lease,
                parent_id=parent, turn_id=origin["trigger_message_id"], now=now,
                error={**inbox.DELIVERY_TERMINAL_ERROR,
                    "message": error["message"] + " No new model request was sent for this continuation.",
                    "reason_code": error["code"], "delivery_attempts": attempt})
            # Original human inputs remain intact, including constraints
            # consumed by an earlier run. The new error has no file body.
            from assistant.results import record_execution_result_locked
            result = await record_execution_result_locked(db, session, lease=lease,
                result_message_id=message.id, inbox_rows=[], outcome="delivery_error", now=message.created_at)
        await append_agent_event_locked(db, session, kind="attachment.delivery_failed",
            payload={"delivery_key": key, "origin": origin, "attempt": attempt,
                "asset_set_digest": request_hash(sorted(set(expected))), "asset_count": len(set(expected)),
                "error": error,
                "result_message_id": message.id if message else None},
            run_fence=fence, turn_id=origin["trigger_message_id"], message_id=parent,
            idempotency_key=f"attachment-delivery:{key}:failed:{attempt}")
    if message is not None:
        from bus import bus
        from bus.events import MESSAGE_CREATED, MESSAGE_UPDATED
        created, updated = inbox.delivery_error_payloads(message)
        base = {"userId": lease.user_id, "sessionId": lease.session_id, "generation": lease.generation}
        bus.publish(MESSAGE_CREATED, {**base, "message": created})
        bus.publish(MESSAGE_UPDATED, {**base, "message": updated})
        if result is not None:
            from assistant.results import on_execution_result_committed
            await on_execution_result_committed(result.id)
        return inbox.AttachmentDeliveryResult((), (), result_message_id=message.id)
    raise inbox.InboxAttachmentDeliveryPending(()) from None


async def deliver_unclaimed_attachments(lease, expected, *, is_assistant):
    from assistant.scheduling import TaskSchedulingHeld
    from sandbox.assets import AssetDeliveryError, deliver_asset_ids
    async with get_db_session() as db:
        session, _, _, previous, originals, _ = await _context_locked(db, lease)
        terminal = _terminal(previous)
        if terminal is not None:
            return terminal
    try:
        if previous and previous.payload["asset_set_digest"] != request_hash(sorted(set(expected))):
            raise AssetDeliveryError(expected_asset_ids=expected, missing_asset_ids=[],
                code="asset_origin_unavailable", retryable=False)
        if is_assistant:
            async with get_db_session() as db:
                await inbox._validate_owned_attachments_locked(db, user_id=lease.user_id,
                    attachment_ids=expected, workspace_id=session.workspace_id)
        elif originals:
            if {asset for item in originals for asset in item.attachments or []} != set(expected):
                raise AssetDeliveryError(expected_asset_ids=expected, missing_asset_ids=[],
                    code="asset_origin_unavailable", retryable=False)
            for item in originals:
                if item.attachments:
                    await deliver_asset_ids(lease.session_id, lease.user_id, item.attachments,
                        expected_asset_ids=item.attachments, delivery_id=item.id)
        else:
            await deliver_asset_ids(lease.session_id, lease.user_id, list(expected),
                strict=True, expected_asset_ids=list(expected))
    except TaskSchedulingHeld:
        raise
    except Exception as exc:
        error = inbox._safe_delivery_failure(exc, expected_asset_ids=expected)
        if isinstance(exc, inbox.InboxAttachmentError):
            error.update(code="asset_unavailable", retryable=False,
                message="The attachment is no longer available.")
        return await _record_failure(lease, expected, error)
    return inbox.AttachmentDeliveryResult((), (), direct_trigger=True)
