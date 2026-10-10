"""Current authority for a socket's original, server-issued identity.

A consumed ticket authenticates the handshake, not the lifetime of a relay.
Check again after waiting for a frame and before handing its bytes to either
peer. The workspace never follows a later default-workspace change.
"""
import asyncio
from dataclasses import dataclass

import anyio
from fastapi import HTTPException
from sqlalchemy import select

from db.base import get_db_session
from db.models.user import User
from session.policy import active_membership


@dataclass(frozen=True)
class SocketAccess:
    user_id: str
    workspace_id: str | None
    client: str | None
    mobile_session_id: str | None
    authenticated: bool

    @classmethod
    def from_ticket(cls, identity: dict, *, authenticated: bool):
        return cls(identity["user_id"], identity.get("workspace_id"),
                   identity.get("client"), identity.get("mobile_session_id"), authenticated)

    async def check(self) -> str:
        """Return the current role, or refuse without exposing resource data."""
        if not self.authenticated:
            return "admin"  # Explicit single-user mode; never ticket-controlled.
        if not self.workspace_id:
            raise HTTPException(403, "A workspace-bound ticket is required")
        # A disconnect also directly cancels pump Tasks. Finish and drain the
        # small SQL read before propagating either asyncio or AnyIO cancellation;
        # cancelling an executing SQLite cursor can otherwise retain a read
        # lock after the TestClient/ASGI event loop has gone away. No socket or
        # remote operation runs under this database-only shield.
        with anyio.CancelScope(shield=True):
            read = asyncio.create_task(self._current_role())
            try:
                return await asyncio.shield(read)
            except asyncio.CancelledError:
                await asyncio.gather(read, return_exceptions=True)
                raise

    async def _current_role(self) -> str:
        from auth.mobile import validate_claims
        await validate_claims({"sub": self.user_id, "client": self.client,
                               "sid": self.mobile_session_id})
        async with get_db_session() as db:
            role = await db.scalar(select(User.role).where(
                User.id == self.user_id, User.is_active.is_(True), User.is_deleted.is_(False),
                active_membership(self.user_id, self.workspace_id),
            ))
        if role is None:
            raise HTTPException(403, "Socket access is no longer available")
        return role

    async def watch(self, interval: float = 5):
        """Close idle sockets too; each active frame has its own fresh check."""
        while True:
            await self.check()
            await asyncio.sleep(interval)
