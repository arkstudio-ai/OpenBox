"""What a reply drew on: the memories recall found relevant to the user's message.

Recall (memory/orchestrator.py ``run_memory_context``) brings two groups each turn: the lasting
background every turn carries, and the memories relevant to this message. Only the second says
anything about this answer, so only it is recorded (agent/loop.py, once per message) and shown
under the reply as "参考了哪些记忆".

Ids only. A read authorizes again with the same check recall makes before every model send
(memory/retrieval.py ``authorized_documents``) and shows only memories still in use, as they read
now: one forgotten since disappears, one whose chat was deleted is withdrawn.
"""
from datetime import datetime, timezone

from core.log import create_logger

log = create_logger("memory.recalls")

LIMIT = 12          # memories kept for one message
SESSION_ROWS = 200  # the chat's most recent messages read back


def recalled_ids(bundle) -> list[str]:
    """The relevant memories in a recall bundle, in its order (wiki pages and sources are not memories)."""
    items = (bundle or {}).get("items") or []
    ids = [item["id"] for item in items if isinstance(item, dict) and item.get("kind") == "memory" and item.get("id")]
    return list(dict.fromkeys(ids))[:LIMIT]


async def record(*, user_id: str, workspace_id: str, session_id: str, message_id: str, bundle) -> None:
    """Keep what recall brought for ``message_id``; a later recall for the same message replaces it."""
    ids = recalled_ids(bundle)
    if not ids:
        return
    from db.base import get_db_session
    from db.models.memory_v2 import MemoryRecall
    async with get_db_session() as db:
        row = await db.get(MemoryRecall, message_id)
        if row is None:
            db.add(MemoryRecall(message_id=message_id, session_id=session_id, user_id=user_id,
                                workspace_id=workspace_id, memory_ids=ids, created_at=datetime.now(timezone.utc)))
        elif row.user_id == user_id:
            row.memory_ids = ids


async def for_session(*, user_id: str, workspace_id: str | None, session_id: str) -> dict[str, list[dict]] | None:
    """{user message id: memories it drew on} for one chat the reader can open; None when they cannot.

    Only the reader's own recalls: in a shared chat, a teammate's memories are never shown.
    """
    from sqlalchemy import select
    from core.config import get_config
    from db.base import get_db_session
    from db.models.memory_v2 import MemoryRecall
    from memory.policy import MemoryAccessDenied, resolve_access_scope
    from memory.retrieval import authorized_documents
    from session.session import get_session, get_session_in_workspace
    session = await get_session(session_id, user_id=user_id)
    if session is None and workspace_id:  # a teammate's chat the reader can open
        session = await get_session_in_workspace(session_id, workspace_id, user_id=user_id)
    if session is None:
        return None
    async with get_db_session() as db:
        rows = (await db.scalars(select(MemoryRecall).where(
            MemoryRecall.user_id == user_id, MemoryRecall.session_id == session_id).order_by(
            MemoryRecall.created_at.desc()).limit(SESSION_ROWS))).all()
        wanted = {("memory", memory_id) for row in rows for memory_id in (row.memory_ids or [])}
        if not wanted:
            return {}
        try:
            access = await resolve_access_scope(db, user_id=user_id, workspace_id=session.workspace_id,
                                                include_all_projects=True)
        except MemoryAccessDenied:
            return {}
        documents = await authorized_documents(db, access, get_config().memory, only=wanted)
    shown = {doc.id: {"id": doc.id, "summary": doc.text, "type": doc.category, "project_id": doc.project_id}
             for doc in documents if doc.kind == "memory"}
    recalls = {row.message_id: [shown[memory_id] for memory_id in row.memory_ids or [] if memory_id in shown]
               for row in rows}
    return {message_id: memories for message_id, memories in recalls.items() if memories}
