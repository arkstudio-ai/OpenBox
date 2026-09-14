"""Session revert: undo file changes by restoring snapshots.

Uses the snapshot system to find step-start snapshots in the message
history and restore the sandbox to that state.
"""
from session.session import get_messages, save_part
from snapshot import snapshot
from core.log import create_logger
import time

log = create_logger("session.revert")

# Store the pre-revert snapshot so unrevert can restore it
_revert_snapshots: dict[str, str] = {}  # session_id -> pre-revert snapshot


async def _begin_restore(session_id: str, user_id: str, *, operation: str,
                         from_snapshot: str | None, to_snapshot: str,
                         message_id: str | None = None):
    from trajectory import enabled, record, context_for_session, mark_capture_paused_in_tx
    from db.base import get_db_session
    if not enabled(user_id):
        from db.base import _engine
        if _engine is not None:
            async with get_db_session() as db:
                await mark_capture_paused_in_tx(db, user_id, session_id)
        return None, None
    from db.models.session import Session
    from core.identifier import ascending
    from session.session import prepare_trajectory_baseline_assets, capture_trajectory_baseline_in_tx
    prepared = await prepare_trajectory_baseline_assets(session_id, user_id)
    async with get_db_session() as db:
        context = await context_for_session(db, user_id, session_id)
        await capture_trajectory_baseline_in_tx(db, context, await db.get(Session, session_id),
                                                 prepared_assets=prepared)
        data = {"operation_id": ascending("restore"), "operation": operation,
                "from_snapshot": from_snapshot, "to_snapshot": to_snapshot,
                "from_message_id": message_id, "capture_level": "snapshot_reference"}
        await record("history.reverted", {**data, "status": "requested"}, context=context, db=db,
                     event_id=f"{data['operation_id']}:requested")
    return context, data


async def _finish_restore(context, data, success: bool, started: float):
    if context is None:
        return
    from trajectory import record
    await record("history.reverted", {**data, "status": "completed" if success else "failed",
                 "duration_ms": (time.monotonic() - started) * 1000,
                 "timing_source": "producer_monotonic"}, context=context,
                 event_id=f"{data['operation_id']}:finished")


async def revert_to_message(session_id: str, message_id: str, *, user_id: str) -> bool:
    """Revert file changes to the state before a message was processed.

    Finds the step-start snapshot for the target message and restores
    the sandbox to that state. Saves the current state for unrevert.
    """
    try:
        msgs = await get_messages(session_id, user_id=user_id)
        if not msgs:
            log.warning(f"No messages found for session {session_id}")
            return False

        # Find the target message and its step-start snapshot
        target_snapshot = None
        found_message = False

        for msg in msgs:
            if msg.id == message_id:
                found_message = True
                # Look for step-start part in this message
                target_snapshot = _find_step_start_snapshot(msg)
                break

        if not found_message:
            # Look for the step-start snapshot in the assistant message
            # that corresponds to the target user message (parentID match)
            for msg in msgs:
                role = msg.role if isinstance(msg.role, str) else msg.role.value
                if role == "assistant" and msg.parent_id == message_id:
                    target_snapshot = _find_step_start_snapshot(msg)
                    if target_snapshot:
                        break

        if not target_snapshot:
            # Fallback: find step-start snapshot just before the message
            for i, msg in enumerate(msgs):
                if msg.id == message_id or (hasattr(msg, 'parent_id') and msg.parent_id == message_id):
                    # Search backwards for the nearest step-start snapshot
                    for j in range(i - 1, -1, -1):
                        snap = _find_step_finish_snapshot(msgs[j])
                        if snap:
                            target_snapshot = snap
                            break
                    break

        if not target_snapshot:
            log.warning(f"No snapshot found for message {message_id}")
            return False

        # Save current state for unrevert
        from sandbox import sandbox_manager
        sandbox = await sandbox_manager.get_client(session_id, user_id=user_id)
        current_snapshot = await snapshot.track(session_id, sandbox)
        if current_snapshot:
            _revert_snapshots[session_id] = current_snapshot

        # Restore to target snapshot
        context, data = await _begin_restore(session_id, user_id, operation="restore",
                                             from_snapshot=current_snapshot,
                                             to_snapshot=target_snapshot, message_id=message_id)
        started = time.monotonic()
        try:
            success = await snapshot.restore(target_snapshot, session_id, sandbox)
        except Exception:
            await _finish_restore(context, data, False, started)
            raise
        await _finish_restore(context, data, success, started)
        if success:
            log.info(f"Reverted session {session_id} to snapshot {target_snapshot[:12]}")
        return success

    except Exception as e:
        from trajectory.types import TrajectoryError
        if isinstance(e, TrajectoryError):
            raise
        log.error(f"Failed to revert session {session_id}: {e}")
        return False


async def unrevert(session_id: str, *, user_id: str) -> bool:
    """Undo a revert by restoring the pre-revert snapshot."""
    try:
        pre_revert = _revert_snapshots.get(session_id)
        if not pre_revert:
            log.warning(f"No revert to undo for session {session_id}")
            return False

        context, data = await _begin_restore(session_id, user_id, operation="undo_restore",
                                             from_snapshot=None, to_snapshot=pre_revert)
        started = time.monotonic()
        try:
            success = await snapshot.restore(pre_revert, session_id, user_id=user_id)
        except Exception:
            await _finish_restore(context, data, False, started)
            raise
        await _finish_restore(context, data, success, started)
        if success:
            _revert_snapshots.pop(session_id, None)
            log.info(f"Unreverted session {session_id} to snapshot {pre_revert[:12]}")
        return success

    except Exception as e:
        from trajectory.types import TrajectoryError
        if isinstance(e, TrajectoryError):
            raise
        log.error(f"Failed to unrevert session {session_id}: {e}")
        return False


def _find_step_start_snapshot(msg) -> str | None:
    """Find a step-start snapshot in a message's parts."""
    for part in (msg.parts or []):
        p = part if isinstance(part, dict) else (part.model_dump() if hasattr(part, "model_dump") else {})
        if isinstance(p, dict) and p.get("type") == "step-start" and p.get("snapshot"):
            return p["snapshot"]
    return None


def _find_step_finish_snapshot(msg) -> str | None:
    """Find a step-finish snapshot in a message's parts."""
    for part in (msg.parts or []):
        p = part if isinstance(part, dict) else (part.model_dump() if hasattr(part, "model_dump") else {})
        if isinstance(p, dict) and p.get("type") == "step-finish" and p.get("snapshot"):
            return p["snapshot"]
    return None
