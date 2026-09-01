"""Preview token — short-lived signed URL token for container preview proxy.

Usage:
  1. Frontend POSTs /api/containers/{id}/preview-token?port=3000.
  2. The response sets a scoped HttpOnly cookie and returns a clean preview URL.
  3. The preview proxy validates the opaque cookie on every request.
"""
import json
import secrets

from core.log import create_logger

log = create_logger("auth.preview")

_cache = None
PREVIEW_TOKEN_TTL = 3600  # 1 hour
_PREVIEW_EPOCH_PREFIX = "pt_epoch:"


def init_preview_store(cache):
    global _cache
    _cache = cache


async def _preview_epoch(user_id: str) -> int:
    """Return the durable revocation generation for one user."""
    value = await _cache.get(f"{_PREVIEW_EPOCH_PREFIX}{user_id}")
    if value is None:
        return 0
    if isinstance(value, bool):
        raise RuntimeError("Invalid preview token revocation epoch")
    try:
        epoch = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Invalid preview token revocation epoch") from exc
    if epoch < 0:
        raise RuntimeError("Invalid preview token revocation epoch")
    return epoch


async def create_preview_token(user_id: str, container_id: str, port: int) -> str:
    """Create a preview token bound to a specific container + port."""
    if _cache is None:
        raise RuntimeError("Preview token store not initialized")
    epoch = await _preview_epoch(user_id)
    token = secrets.token_urlsafe(24)
    await _cache.set(
        f"pt:{token}",
        json.dumps(
            {
                "user_id": user_id,
                "container_id": container_id,
                "port": port,
                "epoch": epoch,
            }
        ),
        ttl=PREVIEW_TOKEN_TTL,
    )
    return token


async def revoke_preview_tokens(user_id: str) -> int:
    """Atomically invalidate every preview token previously issued to a user."""
    if _cache is None:
        raise RuntimeError("Preview token store not initialized")
    return await _cache.incr(f"{_PREVIEW_EPOCH_PREFIX}{user_id}")


async def get_preview_token_claims(token: str) -> dict | None:
    """Load validated claims for an opaque preview token.

    The token itself is the credential, so callers must never forward it to a
    sandbox application or include it in logs.  Binding the returned claims to
    the requested container and port remains the caller's responsibility.
    """
    if _cache is None or not token:
        return None

    data = await _cache.get(f"pt:{token}")
    if data is None:
        return None

    try:
        if isinstance(data, str):
            data = json.loads(data)
    except (TypeError, ValueError):
        log.warning("Ignoring malformed preview token claims")
        return None

    if not isinstance(data, dict):
        return None

    user_id = data.get("user_id")
    container_id = data.get("container_id")
    port = data.get("port")
    epoch = data.get("epoch")
    if (
        not isinstance(user_id, str)
        or not user_id
        or not isinstance(container_id, str)
        or not container_id
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch < 0
    ):
        return None

    try:
        if epoch != await _preview_epoch(user_id):
            return None
    except RuntimeError:
        log.warning("Ignoring preview token because its revocation epoch is invalid")
        return None

    return {"user_id": user_id, "container_id": container_id, "port": port}


async def verify_preview_token(token: str, container_id: str, port: int) -> dict | None:
    """Verify a preview token. Returns {user_id, container_id, port} or None."""
    data = await get_preview_token_claims(token)
    if data is None:
        return None
    # Verify token is bound to this container + port
    if data.get("container_id") != container_id or data.get("port") != port:
        return None
    return data
