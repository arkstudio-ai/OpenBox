"""Session forking: duplicate a session up to a given message.

Creates a new session with copies of all messages and parts up to
a specified message_id, allowing the user to branch the conversation.
"""
from datetime import datetime, timezone

from core.identifier import ascending
from core.log import create_logger

log = create_logger("session.fork")


async def fork_session(
    source_session_id: str,
    up_to_message_id: str | None = None,
    user_id: str = "default",
) -> dict:
    """Fork a session, copying messages up to the specified message.

    Args:
        source_session_id: The session to fork from.
        up_to_message_id: Copy messages up to (and including) this message.
                          If None, copies all messages.
        user_id: The user performing the fork.

    Returns:
        The newly created session dict.
    """
    from session.session import get_session, create_session, get_messages

    source = await get_session(source_session_id, user_id=user_id)
    if not source:
        raise ValueError(f"Session {source_session_id} not found")

    # Get messages from source
    messages = await get_messages(source_session_id, user_id=user_id)

    # Truncate at the specified message
    if up_to_message_id:
        truncated = []
        for msg in messages:
            truncated.append(msg)
            if msg.id == up_to_message_id:
                break
        messages = truncated

    # Create new session with same model/agent
    new_session = await create_session(
        model=source.model,
        agent=source.agent or "build",
        variant=source.variant,
        title=f"Fork: {source.title or 'Untitled'}",
        user_id=user_id,
        workspace_id=source.workspace_id,
        project_id=getattr(source, "project_id", None),
    )

    # Deep copy messages and parts to new session
    now = datetime.now(timezone.utc)

    def _use_db() -> bool:
        try:
            from db.base import _engine
            return _engine is not None
        except ImportError:
            return False

    if _use_db():
        from sqlalchemy import select
        from db.base import get_db_session
        from db.models.message import Message as MessageORM
        from db.models.part import Part as PartORM
        from db.models.session import Session as SessionORM
        from trajectory import TraceContext, context_for_session, enabled, record
        from session.session import prepare_trajectory_baseline_assets, capture_trajectory_baseline_in_tx

        # Fork history is an explicit baseline, never a replay of old activity.
        # Retain assets before acquiring either trajectory's write lock.
        prepared_assets = await prepare_trajectory_baseline_assets(
            source_session_id, user_id, root_session_id=new_session.id)

        async with get_db_session() as db:
            message_mapping = {}
            source_part_ids = []
            for message in messages:
                for source_part in message.parts or []:
                    source_data = source_part if isinstance(source_part, dict) else (
                        source_part.model_dump() if hasattr(source_part, "model_dump") else {}
                    )
                    if isinstance(source_data, dict) and source_data.get("id"):
                        source_part_ids.append(source_data["id"])
            identity_by_part: dict[str, dict] = {}
            if source_part_ids:
                source_rows = (await db.execute(
                    select(PartORM).where(
                        PartORM.id.in_(source_part_ids),
                        PartORM.session_id == source_session_id,
                        PartORM.user_id == user_id,
                    )
                )).scalars().all()
                for source_row in source_rows:
                    values = {
                        "canonical_tool_id": source_row.canonical_tool_id,
                        "wire_tool_name": source_row.wire_tool_name,
                        "provider_binding_digest": source_row.provider_binding_digest,
                        "provider_dialect": source_row.provider_dialect,
                    }
                    present = [value is not None for value in values.values()]
                    if any(present) and (
                        not all(present) or source_row.stream_seq is None
                    ):
                        raise ValueError(
                            "Cannot fork a transcript with partial ToolPart identity"
                        )
                    if all(present):
                        identity_by_part[source_row.id] = {
                            **values,
                            "stream_seq": source_row.stream_seq,
                        }

            for msg in messages:
                new_msg_id = ascending("message")
                message_mapping[msg.id] = new_msg_id
                role = msg.role if isinstance(msg.role, str) else msg.role.value

                msg_row = MessageORM(
                    id=new_msg_id,
                    session_id=new_session.id,
                    user_id=user_id,
                    role=role,
                    agent=getattr(msg, "agent", None),
                    model=getattr(msg, "model", None),
                    model_id=getattr(msg, "model_id", None),
                    finish=getattr(msg, "finish", None),
                    created_at=now,
                )
                db.add(msg_row)

                # Copy parts
                parts = msg.parts or []
                for part in parts:
                    p = part if isinstance(part, dict) else (
                        part.model_dump() if hasattr(part, "model_dump") else {}
                    )
                    if not isinstance(p, dict):
                        continue

                    new_part_id = ascending("part")
                    source_part_id = p.get("id", "")
                    p_copy = {
                        **p,
                        "id": new_part_id,
                        "message_id": new_msg_id,
                        "session_id": new_session.id,
                    }

                    part_row = PartORM(
                        id=new_part_id,
                        message_id=new_msg_id,
                        session_id=new_session.id,
                        user_id=user_id,
                        type=p.get("type", "text"),
                        data=p_copy,
                        **identity_by_part.get(source_part_id, {}),
                        created_at=now,
                    )
                    db.add(part_row)

            if enabled(user_id):
                # The source is recorded before its fresh destination, which
                # cannot be concurrently running before this call returns.
                await db.flush()
                source_context = await context_for_session(db, user_id, source_session_id)
                source_row = await db.get(SessionORM, source_session_id)
                await capture_trajectory_baseline_in_tx(db, source_context, source_row,
                                                         prepared_assets=prepared_assets)
                from db.models.trajectory import SessionTrajectory
                source_trajectory = await db.scalar(select(SessionTrajectory).where(
                    SessionTrajectory.session_id == source_context.session_id,
                    SessionTrajectory.user_id == user_id))
                relation = {"source_session_id": source_session_id, "target_session_id": new_session.id,
                            "source_trajectory_id": source_trajectory.id,
                            "source_through_seq": str(source_trajectory.committed_seq),
                            "up_to_message_id": up_to_message_id, "copied_messages": message_mapping,
                            "history_mode": "baseline_only"}
                await record("history.forked", {**relation, "direction": "outgoing"},
                             context=source_context, db=db, event_id=f"fork:{new_session.id}:source")
                destination_context = TraceContext(user_id, new_session.id, workspace_id=source.workspace_id)
                destination_row = await db.get(SessionORM, new_session.id)
                await capture_trajectory_baseline_in_tx(db, destination_context, destination_row,
                                                         prepared_assets=prepared_assets)
                await record("history.forked", {**relation, "direction": "incoming"},
                             context=destination_context, db=db, event_id=f"fork:{new_session.id}:destination")
            else:
                from trajectory import mark_capture_paused_in_tx
                await mark_capture_paused_in_tx(db, user_id, source_session_id)
    else:
        # File-based storage: copy via storage module
        from storage import storage
        for msg in messages:
            new_msg_id = ascending("message")
            role = msg.role if isinstance(msg.role, str) else msg.role.value

            msg_data = {
                "id": new_msg_id,
                "session_id": new_session.id,
                "role": role,
                "agent": getattr(msg, "agent", None),
                "model": getattr(msg, "model", None),
                "finish": getattr(msg, "finish", None),
                "created_at": now.isoformat(),
            }
            # Read existing messages list and append
            existing = await storage.read(["messages", new_session.id]) or []
            existing.append(msg_data)
            await storage.write(["messages", new_session.id], existing)

            # Copy parts
            parts = msg.parts or []
            for part in parts:
                p = part if isinstance(part, dict) else (
                    part.model_dump() if hasattr(part, "model_dump") else {}
                )
                if not isinstance(p, dict):
                    continue
                new_part_id = ascending("part")
                p_copy = {**p, "id": new_part_id, "message_id": new_msg_id, "session_id": new_session.id}
                existing_parts = await storage.read(["parts", new_session.id]) or []
                existing_parts.append(p_copy)
                await storage.write(["parts", new_session.id], existing_parts)

    log.info(f"Forked session {source_session_id} -> {new_session.id} ({len(messages)} messages)")
    return new_session
