"""PostgreSQL/SQLite repository for per-workspace cloud desktops."""
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.identifier import ascending
from db.base import get_db_session
from db.models.cloud_desktop import CloudDesktop


class PgCloudDesktopRepo:
    def __init__(self) -> None:
        # The database unique constraint arbitrates across workers.  This lock
        # avoids needless collisions within one process and also gives SQLite
        # tests the transaction isolation its single in-memory connection lacks.
        self._port_lock = asyncio.Lock()

    async def create(
        self,
        workspace_id: str,
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
