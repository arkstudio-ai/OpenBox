"""PostgreSQL/SQLite repository for per-user cloud desktops."""
from datetime import datetime, timezone

from sqlalchemy import select, update

from core.identifier import ascending
from db.base import get_db_session
from db.models.cloud_desktop import CloudDesktop


class PgCloudDesktopRepo:
    async def create(self, user_id: str, region_id: str, status: str = "creating", **fields) -> dict:
        now = datetime.now(timezone.utc)
        row = CloudDesktop(
            id=ascending("cld"),
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

    async def get_for_user(self, user_id: str) -> dict | None:
        async with get_db_session() as session:
            result = await session.execute(
                select(CloudDesktop).where(
                    CloudDesktop.user_id == user_id,
                    CloudDesktop.is_deleted == False,
                )
            )
            row = result.scalar_one_or_none()
            return _to_dict(row) if row else None

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
