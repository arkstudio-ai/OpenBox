"""PostgreSQL/SQLite repository for per-workspace cloud desktops."""
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.identifier import ascending
from db.base import get_db_session
from db.models.cloud_desktop import CloudDesktop


# A health response belongs to this exact physical assignment and credential
# version. Incidental status/expiry/last-seen updates are not channel identity.
_CHANNEL_PROBE_IDENTITY = (
    "id", "desktop_id", "region_id", "workspace_id", "assigned_at", "pool_state",
    "channel_kind", "private_ip", "tunnel_bind", "tunnel_port",
    "tunnel_fingerprint", "tunnel_pubkey", "action_api_key_hash", "action_api_key_ciphertext",
)


class PgCloudDesktopRepo:
    def __init__(self) -> None:
        # The database unique constraint arbitrates across workers.  This lock
        # avoids needless collisions within one process and also gives SQLite
        # tests the transaction isolation its single in-memory connection lacks.
        self._port_lock = asyncio.Lock()
        self._pool_lock = asyncio.Lock()

    async def create(
        self,
        workspace_id: str | None,
        region_id: str,
        status: str = "creating",
        *,
        user_id: str | None = None,
        **fields,
    ) -> dict:
        now = datetime.now(timezone.utc)
        row = CloudDesktop(
            id=ascending("cld"),
            workspace_id=workspace_id,
            user_id=user_id,
            region_id=region_id,
            status=status,
            created_at=now,
            updated_at=now,
            **fields,
        )
        async with get_db_session() as session:
            session.add(row)
        return _to_dict(row)

    async def get_for_workspace(self, workspace_id: str) -> dict | None:
        async with get_db_session() as session:
            result = await session.execute(
                select(CloudDesktop).where(
                    CloudDesktop.workspace_id == workspace_id,
                    CloudDesktop.is_deleted == False,
                )
            )
            row = result.scalar_one_or_none()
            return _to_dict(row) if row else None

    async def get(self, record_id: str) -> dict | None:
        async with get_db_session() as session:
            row = await session.get(CloudDesktop, record_id)
            return _to_dict(row) if row and not row.is_deleted else None

    async def get_by_desktop_id(self, desktop_id: str) -> dict | None:
        async with get_db_session() as session:
            result = await session.execute(
                select(CloudDesktop).where(
                    CloudDesktop.desktop_id == desktop_id,
                    CloudDesktop.is_deleted == False,
                )
            )
            row = result.scalars().first()
            return _to_dict(row) if row else None

    async def get_any_by_desktop_id(self, desktop_id: str) -> dict | None:
        async with get_db_session() as session:
            result = await session.execute(
                select(CloudDesktop)
                .where(CloudDesktop.desktop_id == desktop_id)
                .order_by(CloudDesktop.updated_at.desc())
            )
            row = result.scalars().first()
            return _to_dict(row) if row else None

    async def get_by_fingerprint(self, fingerprint: str) -> dict | None:
        async with get_db_session() as session:
            result = await session.execute(
                select(CloudDesktop).where(
                    CloudDesktop.tunnel_fingerprint == fingerprint,
                    CloudDesktop.is_deleted == False,
                )
            )
            row = result.scalar_one_or_none()
            return _to_dict(row) if row else None

    async def list_active(self) -> list[dict]:
        async with get_db_session() as session:
            result = await session.execute(
                select(CloudDesktop).where(CloudDesktop.is_deleted == False)
            )
            return [_to_dict(row) for row in result.scalars().all()]

    async def list_pool_state(self, pool_state: str) -> list[dict]:
        async with get_db_session() as session:
            result = await session.execute(
                select(CloudDesktop).where(
                    CloudDesktop.pool_state == pool_state,
                    CloudDesktop.is_deleted.is_(False),
                )
            )
            return [_to_dict(row) for row in result.scalars().all()]

    async def claim_prewarm(
        self, workspace_id: str, triggered_by_user_id: str | None,
        *, usable_until: datetime | None = None,
    ) -> dict | None:
        """Atomically claim the newest-expiring prewarm desktop."""
        async with self._pool_lock:
            async with get_db_session() as session:
                current = await session.scalar(
                    select(CloudDesktop).where(
                        CloudDesktop.workspace_id == workspace_id,
                        CloudDesktop.is_deleted.is_(False),
                    )
                )
                if current is not None:
                    return _to_dict(current)
                row = await session.scalar(
                    select(CloudDesktop)
                    .where(
                        CloudDesktop.pool_state == "prewarm",
                        CloudDesktop.workspace_id.is_(None),
                        CloudDesktop.is_deleted.is_(False),
                        *([CloudDesktop.expires_at > usable_until,
                           CloudDesktop.charge_type == "PrePaid"] if usable_until else []),
                    )
                    .order_by(CloudDesktop.expires_at.desc(), CloudDesktop.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    return None
                now = datetime.now(timezone.utc)
                row.pool_state = "assigning"
                row.workspace_id = workspace_id
                row.user_id = triggered_by_user_id
                row.updated_at = now
                await session.flush()
                return _to_dict(row)

    async def reserve_tunnel_port(self, record_id: str, low: int, high: int) -> int:
        """Reserve the lowest free port, retrying a concurrent unique clash."""
        if not 1 <= low <= high <= 65535:
            raise ValueError("invalid WUYING_TUNNEL_PORT_RANGE")
        async with self._port_lock:
            return await self._reserve_tunnel_port_locked(record_id, low, high)

    async def _reserve_tunnel_port_locked(self, record_id: str, low: int, high: int) -> int:
        for _attempt in range(high - low + 1):
            try:
                async with get_db_session() as session:
                    current = await session.scalar(
                        select(CloudDesktop).where(CloudDesktop.id == record_id).with_for_update()
                    )
                    if current is None or current.is_deleted:
                        raise LookupError(f"cloud desktop record not found: {record_id}")
                    if current.tunnel_port is not None:
                        return current.tunnel_port
                    used_result = await session.execute(
                        select(CloudDesktop.tunnel_port).where(
                            CloudDesktop.tunnel_port.is_not(None),
                        )
                    )
                    used = {port for port in used_result.scalars() if port is not None}
                    port = next((candidate for candidate in range(low, high + 1) if candidate not in used), None)
                    if port is None:
                        raise RuntimeError("WUYING tunnel port range exhausted")
                    current.tunnel_port = port
                    current.updated_at = datetime.now(timezone.utc)
                    await session.flush()  # unique constraint is the cross-worker arbiter
                    return port
            except IntegrityError:
                continue
        raise RuntimeError("could not reserve a WUYING tunnel port after concurrent conflicts")

    async def update(self, record_id: str, **fields) -> None:
        fields["updated_at"] = datetime.now(timezone.utc)
        async with get_db_session() as session:
            await session.execute(
                update(CloudDesktop).where(CloudDesktop.id == record_id).values(**fields)
            )

    async def record_channel_probe(self, expected: dict, *, healthy: bool, error: str | None = None) -> bool:
        """Commit probe health only if its original live channel still exists.

        No transaction spans the network probe. A single conditional UPDATE
        arbitrates with revoke/reassignment, including a concurrent uncommitted
        change, without a read-then-write authorization window.
        """
        state = expected.get("tunnel_state")
        if (state not in ("pending", "up", "down") or expected.get("is_deleted")
                or any(name not in expected for name in _CHANNEL_PROBE_IDENTITY)):
            return False
        if not healthy and state != "up":
            return False
        now = datetime.now(timezone.utc)
        fields = {"tunnel_state": "up" if healthy else "down", "updated_at": now,
                  "channel_error": None if healthy else error}
        if healthy:
            fields["last_seen_at"] = now
        async with get_db_session() as session:
            result = await session.execute(
                update(CloudDesktop).where(
                    *(getattr(CloudDesktop, name) == expected[name] for name in _CHANNEL_PROBE_IDENTITY),
                    CloudDesktop.is_deleted.is_(False),
                    CloudDesktop.tunnel_state == state,
                ).values(**fields).execution_options(synchronize_session=False)
            )
        return result.rowcount == 1

    async def soft_delete(self, record_id: str) -> None:
        now = datetime.now(timezone.utc)
        async with get_db_session() as session:
            await session.execute(
                update(CloudDesktop)
                .where(CloudDesktop.id == record_id)
                .values(is_deleted=True, deleted_at=now, updated_at=now)
            )


def _to_dict(row: CloudDesktop) -> dict:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


cloud_desktop_repo = PgCloudDesktopRepo()
