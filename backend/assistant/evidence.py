"""Source references, checked against current ownership only.

V2 (docs/PERSONAL_ASSISTANT_DESIGN_V2.md, D1): a saved answer is not
re-validated when it is read again, and revocation is not retroactive. A
reference resolves while its part still exists in a live session that the
actor owns in this workspace; nothing walks the answer's provenance.
"""
import json

from sqlalchemy import select

from assistant.commands import command_digest
from assistant.policy import AssistantError
from assistant.results import validate_source_asset
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session


def projection_digest(value: dict) -> str:
    return command_digest(json.loads(json.dumps(value, default=str)))


async def validate_source_ref(db, ref, *, user_id, workspace_id, main_id=None, **_ignored):
    """Return the referenced Part if the actor still owns it."""
    if not isinstance(ref, dict):
        raise AssistantError(410, "ASSISTANT_SOURCE_UNAVAILABLE", "Original evidence is unavailable")
    part = await db.scalar(select(Part).join(Message, Message.id == Part.message_id)
        .join(Session, Session.id == Part.session_id).where(
            Part.id == ref.get("part_id"), Part.message_id == ref.get("message_id"),
            Part.session_id == ref.get("session_id"), Part.user_id == user_id,
            Message.user_id == user_id, Session.user_id == user_id,
            Session.workspace_id == workspace_id, Session.is_deleted.is_(False)))
    if part is None:
        raise AssistantError(410, "ASSISTANT_SOURCE_UNAVAILABLE", "Original evidence is unavailable")
    await validate_source_asset(db, part, user_id=user_id, workspace_id=workspace_id)
    return part
