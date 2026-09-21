"""API keys for the public ``/v1`` harness API.

A key looks like ``obx_sk_<43 urlsafe chars>``. The secret is shown exactly
once, at issuance; only its SHA-256 is stored. Authentication resolves the
hash to a key row, checks the row and its owner are still live, and returns
the same identity dict the JWT path returns — plus the fields that make the
key's workspace and policy available downstream without another lookup.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.api_key import ApiKey
from db.models.user import User
from db.models.workspace import WorkspaceMember

log = create_logger("auth.api_key")

KEY_PREFIX = "obx_sk_"
#: Characters of the secret kept beside the hash so listings can name a key.
_VISIBLE_CHARS = 8

SCOPES = frozenset({"sessions:read", "sessions:write", "files:read", "files:write"})
DEFAULT_SCOPES = ("sessions:read", "sessions:write", "files:read", "files:write")

#: Unattended-interaction policy and per-key limits. Task C reads
#: ``permission`` / ``question`` / ``interaction_timeout_s`` /
#: ``allowed_tools``; the ``/v1`` layer reads ``max_concurrent_sessions``.
DEFAULT_POLICY: dict = {
    "permission": "auto_allow",
    "question": "auto_reject",
    "interaction_timeout_s": 600,
    "allowed_tools": [],
    "max_concurrent_sessions": 5,
}

#: ``last_used_at`` is refreshed at most this often per key.
_TOUCH_INTERVAL = timedelta(seconds=60)
_last_touch: dict[str, datetime] = {}


def is_api_key(token: str | None) -> bool:
    return bool(token) and token.startswith(KEY_PREFIX)


def generate_secret() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def key_prefix_of(secret: str) -> str:
    return secret[: len(KEY_PREFIX) + _VISIBLE_CHARS]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def normalize_policy(policy: dict | None) -> dict:
    """Fill defaults so readers never branch on a missing key."""
    merged = dict(DEFAULT_POLICY)
    merged.update({k: v for k, v in (policy or {}).items() if v is not None})
    return merged


def validate_scopes(scopes: list[str] | tuple[str, ...]) -> list[str]:
    unknown = sorted(set(scopes) - SCOPES)
    if unknown:
        raise ValueError(f"unknown scopes: {', '.join(unknown)}")
    return sorted(set(scopes))


async def issue_key(
    *,
    user_id: str,
    workspace_id: str,
    name: str,
    scopes: list[str] | tuple[str, ...] = DEFAULT_SCOPES,
    policy: dict | None = None,
    rate_limit: str | None = None,
    expires_at: datetime | None = None,
) -> tuple[ApiKey, str]:
    """Create a key. Returns the row and the one-time plaintext secret."""
    scopes = validate_scopes(scopes)
    async with get_db_session() as db:
        member = await db.scalar(select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.status == "active",
        ))
        if member is None:
            raise LookupError(f"user {user_id} is not an active member of workspace {workspace_id}")
        secret = generate_secret()
        now = datetime.now(timezone.utc)
        row = ApiKey(
            id=ascending("key"),
            user_id=user_id,
            workspace_id=workspace_id,
            name=name,
            key_prefix=key_prefix_of(secret),
            key_hash=hash_secret(secret),
            scopes=scopes,
            policy=normalize_policy(policy),
            rate_limit=rate_limit,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        await db.flush()
    return row, secret


async def revoke_key(key_id: str) -> bool:
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        result = await db.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
            .values(revoked_at=now, updated_at=now)
        )
        return bool(result.rowcount)


def identity_for(row: ApiKey, member_role: str) -> dict:
    """The ``current_user`` dict for a request authenticated by ``row``.

    Keys are never admins, whatever their owner is: the public API exposes
    member-level capability only.
    """
    return {
        "user_id": row.user_id,
        "role": "user",
        "workspace_id": row.workspace_id,
        "workspace_role": member_role,
        "auth_kind": "api_key",
        "api_key_id": row.id,
        "scopes": list(row.scopes or []),
        "policy": normalize_policy(row.policy),
        "rate_limit": row.rate_limit,
    }


async def authenticate(secret: str) -> dict | None:
    """Resolve a bearer secret to an identity, or ``None`` when it is not valid.

    A revoked or expired key, a deleted or inactive owner, and a lapsed
    workspace membership all fail closed.
    """
    if not is_api_key(secret):
        return None
    digest = hash_secret(secret)
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        row = await db.scalar(select(ApiKey).where(ApiKey.key_hash == digest))
        if row is None:
            return None
        if row.revoked_at is not None:
            log.info(f"Revoked API key used key={row.id}")
            return None
        expires_at = _utc(row.expires_at)
        if expires_at is not None and expires_at <= now:
            log.info(f"Expired API key used key={row.id}")
            return None
        user = await db.get(User, row.user_id)
        if user is None or user.is_deleted or not user.is_active:
            return None
        member = await db.scalar(select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == row.workspace_id,
            WorkspaceMember.user_id == row.user_id,
            WorkspaceMember.status == "active",
        ))
        if member is None:
            return None
        identity = identity_for(row, member.role)
        last = _last_touch.get(row.id)
        if last is None or now - last >= _TOUCH_INTERVAL:
            _last_touch[row.id] = now
            await db.execute(
                update(ApiKey).where(ApiKey.id == row.id).values(last_used_at=now)
            )
    return identity
