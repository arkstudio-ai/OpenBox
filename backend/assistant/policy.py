"""Actor-bound assistant authority. Model parameters never select an owner."""
from sqlalchemy import select

from db.models.session import Session
from db.models.user import User
from session.policy import active_membership, readable_session


class AssistantError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


async def lock_actor(db, user_id: str) -> None:
    # Serialize Command admission without blocking the KEY SHARE locks taken
    # by event/grant foreign keys. FOR UPDATE would deadlock against a worker
    # holding the execution Session while another admission waits for it.
    actor = await db.scalar(select(User).where(User.id == user_id,
        User.is_active.is_(True), User.is_deleted.is_(False)).with_for_update(key_share=True))
    if actor is None:
        raise AssistantError(403, "ASSISTANT_ACTOR_UNAVAILABLE", "Actor is unavailable")


async def require_membership(db, user_id: str, workspace_id: str) -> None:
    if not await db.scalar(select(active_membership(user_id, workspace_id))):
        raise AssistantError(403, "ASSISTANT_WORKSPACE_FORBIDDEN", "Active workspace membership is required")


async def main_session_locked(db, user_id: str, workspace_id: str, *, lock: bool = False):
    statement = select(Session).where(
        readable_session(user_id, workspace_id), Session.user_id == user_id,
        Session.kind == "assistant", Session.visibility == "private",
    )
    if lock:
        statement = statement.with_for_update()
    return await db.scalar(statement)
