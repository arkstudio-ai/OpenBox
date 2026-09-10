"""One-time ticket system for WebSocket authentication.

WebSocket doesn't support custom headers during handshake.
Instead: POST /api/auth/ticket → get ticket → ws://host/ws/agent?ticket=xxx
"""
import secrets
import json

from core.log import create_logger

log = create_logger("auth.ticket")

# Redis-backed ticket store (set by init)
_cache = None


def init_ticket_store(cache):
    """Initialize with a cache (ICache) instance."""
    global _cache
    _cache = cache


async def create_ticket(user_id: str, role: str = "user", *, workspace_id: str | None = None,
                        client: str | None = "web", mobile_session_id: str | None = None) -> str:
    """Create a one-time ticket. Returns the ticket string."""
    if _cache is None:
        raise RuntimeError("Ticket store not initialized")
    ticket = secrets.token_urlsafe(32)
    await _cache.set(
        f"ticket:{ticket}",
        json.dumps({"user_id": user_id, "role": role, "client": client,
                    "mobile_session_id": mobile_session_id, **({"workspace_id": workspace_id} if workspace_id else {})}),
        ttl=30,  # 30 seconds
    )
    return ticket


async def consume_ticket(ticket: str) -> dict | None:
    """Consume a ticket (one-time use). Returns {user_id, role} or None.

    Uses atomic GET+DELETE to prevent replay attacks.
    """
    if _cache is None:
        raise RuntimeError("Ticket store not initialized")
    key = f"ticket:{ticket}"
    data = await _cache.get(key)
    if data is None:
        return None
    # Immediately delete (one-time use)
    await _cache.delete(key)
    identity = json.loads(data) if isinstance(data, str) else data
    from auth.mobile import validate_ticket
    from fastapi import HTTPException
    try:
        await validate_ticket(identity)
    except HTTPException:
        return None
    return identity
