"""JWT token creation and verification — dual token (access + refresh)."""
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from core.log import create_logger

log = create_logger("auth.jwt")

ALGORITHM = "HS256"

# These will be set by init_auth()
_secret: str = ""
_access_expire_minutes: int = 15
_refresh_expire_days: int = 7


def init_auth(secret: str, access_expire_minutes: int = 15, refresh_expire_days: int = 7):
    global _secret, _access_expire_minutes, _refresh_expire_days
    if not secret:
        raise RuntimeError("JWT_SECRET is required for authentication")
    _secret = secret
    _access_expire_minutes = access_expire_minutes
    _refresh_expire_days = refresh_expire_days
    log.info("Auth system initialized")


def create_access_token(user_id: str, role: str = "user") -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=_access_expire_minutes)
    payload = {
        "sub": user_id,
        "role": role,
        "type": "access",
        "exp": expire,
    }
    return jwt.encode(payload, _secret, algorithm=ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=_refresh_expire_days)
    payload = {
        "sub": user_id,
        "type": "refresh",
        "exp": expire,
    }
    return jwt.encode(payload, _secret, algorithm=ALGORITHM)


def create_asset_download_token(
    user_id: str,
    asset_id: str,
    *,
    expire_hours: int = 24,
) -> str:
    """Create a bounded bearer capability for one owned asset download.

    A normal Markdown link navigation cannot carry the SPA's Authorization
    header.  This purpose-specific token lets that one link resolve while
    keeping the asset endpoint owner-bound and time-limited.
    """
    expire = datetime.now(timezone.utc) + timedelta(hours=expire_hours)
    payload = {
        "sub": user_id,
        "asset_id": asset_id,
        "type": "asset_download",
        "exp": expire,
    }
    return jwt.encode(payload, _secret, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    """Decode and verify a JWT token. Returns payload or None if invalid."""
    try:
        payload = jwt.decode(token, _secret, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def decode_access_token(token: str) -> dict | None:
    """Decode an access token specifically. Returns None if invalid or wrong type."""
    payload = decode_token(token)
    if payload and payload.get("type") == "access":
        return payload
    return None


def decode_refresh_token(token: str) -> dict | None:
    """Decode a refresh token specifically. Returns None if invalid or wrong type."""
    payload = decode_token(token)
    if payload and payload.get("type") == "refresh":
        return payload
    return None


def decode_asset_download_token(token: str, asset_id: str) -> dict | None:
    """Verify a download capability is valid for exactly ``asset_id``."""
    payload = decode_token(token)
    if (
        payload
        and payload.get("type") == "asset_download"
        and payload.get("asset_id") == asset_id
        and payload.get("sub")
    ):
        return payload
    return None
