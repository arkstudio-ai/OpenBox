"""Read-only authority for the physical channel a relay first resolved.

An established socket must not follow a replacement assignment or survive a
durable channel revocation merely because stopping the guest tunnel failed.
This is channel authorization, not resource epoch admission or proof that an
already dispatched command (or its descendants) has stopped.
"""
import asyncio
from dataclasses import dataclass, field
from hashlib import sha256

import anyio
from sqlalchemy import select

from db.base import get_db_session
from db.models.cloud_desktop import CloudDesktop


WATCH_INTERVAL_SECONDS = 5.0
_IDENTITY_FIELDS = (
    "id", "desktop_id", "region_id", "workspace_id", "assigned_at",
    "channel_kind", "private_ip", "tunnel_bind", "tunnel_port",
    "tunnel_fingerprint", "tunnel_pubkey", "action_api_key_hash",
)
_READ_FIELDS = (*_IDENTITY_FIELDS, "action_api_key_ciphertext", "is_deleted", "pool_state", "tunnel_state")


def _identity(record):
    # Retain neither the service credential nor its encrypted representation
    # in the binding. Ciphertext rotation also invalidates an existing socket.
    key_version = sha256((record.get("action_api_key_ciphertext") or "").encode()).digest()
    return tuple(record.get(name) for name in _IDENTITY_FIELDS) + (key_version,)


def _live(record):
    return (record is not None and not record.get("is_deleted")
        and record.get("pool_state") == "assigned" and record.get("tunnel_state") == "up")


async def _finish_read(read):
    # Pump cancellation must finish the short SQL read and return its
    # connection before teardown. No socket IO or write lock is in this scope.
    with anyio.CancelScope(shield=True):
        task = asyncio.create_task(read)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise


@dataclass(frozen=True)
class TerminalChannelAccess:
    record_id: str
    identity: tuple = field(repr=False)

    async def _check(self):
        # One primary-key SELECT per check, using a new short transaction.
        # No row lock, long-lived reader, or cross-frame authorization cache.
        async with get_db_session() as db:
            record = (await db.execute(select(*(getattr(CloudDesktop, name) for name in _READ_FIELDS))
                .where(CloudDesktop.id == self.record_id))).mappings().one_or_none()
        if not _live(record) or _identity(record) != self.identity:
            raise PermissionError("Desktop channel access is no longer available")

    async def check(self):
        await _finish_read(self._check())

    async def watch(self):
        while True:
            await self.check()
            await asyncio.sleep(WATCH_INTERVAL_SECONDS)


async def resolve_terminal_channel(provider, container_id, owner):
    """Return a route and its binding from the same first SQL resolution.

Other providers and the legacy shared Wuying endpoint keep their existing
contract; they have no per-desktop SQL channel to pin or revoke here.
"""
    from sandbox.wuying import WuyingProvider
    if not isinstance(provider, WuyingProvider) or not provider.routes_per_user:
        return await provider.get_container(container_id, user_id=owner), None

    from db.repository.cloud_desktop_repo import cloud_desktop_repo
    from sandbox.channel import ChannelConfigError, ChannelNotReady
    from sandbox.entitlement import require_sandbox_subscription

    record = await _finish_read(cloud_desktop_repo.get_by_desktop_id(container_id))
    if record is None:
        raise ValueError("Container not found")
    if record["workspace_id"] != owner or not _live(record):
        raise PermissionError("Desktop channel access is no longer available")
    access = TerminalChannelAccess(record["id"], _identity(record))
    # Preserve the provider's workspace ownership and current paid-access
    # checks. The subsequent check compares this original snapshot, never a
    # second resolution that could silently adopt a replacement desktop.
    await require_sandbox_subscription(owner)
    try:
        info = provider._record_container(record)
    except (ChannelConfigError, ChannelNotReady) as exc:
        raise PermissionError("Desktop channel access is no longer available") from exc
    return info, access


async def resolve_browser_channel(provider, owner):
    """Resolve the extension's original route and binding in one SQL read."""
    from sandbox.wuying import WuyingProvider
    if not isinstance(provider, WuyingProvider) or not provider.routes_per_user:
        return await provider.resolve_user_container(owner), None

    from db.repository.cloud_desktop_repo import cloud_desktop_repo
    from sandbox.channel import ChannelConfigError, ChannelNotReady
    from sandbox.entitlement import require_sandbox_subscription
    from sandbox.wuying_desktop_service import DesktopNotReady

    await require_sandbox_subscription(owner)
    record = await _finish_read(cloud_desktop_repo.get_for_workspace(owner))
    if record is None:
        raise DesktopNotReady({"state": "not_provisioned"})
    if record.get("status") != "running" or not _live(record):
        raise PermissionError("Desktop channel access is no longer available")
    access = TerminalChannelAccess(record["id"], _identity(record))
    try:
        info = provider._record_container(record)
    except (ChannelConfigError, ChannelNotReady) as exc:
        raise PermissionError("Desktop channel access is no longer available") from exc
    # Preserve the provider's existing registry behavior, using exactly the
    # record that supplied this route rather than resolving a replacement.
    provider._containers[info.id] = info
    provider._api_keys[info.id] = info.api_key or ""
    provider._container_owners[info.id] = owner
    provider._container_projects[info.id] = "default"
    return info, access
