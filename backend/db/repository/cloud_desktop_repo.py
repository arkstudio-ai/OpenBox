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
_ASSIGNMENT_FIELDS = (
    "id", "desktop_id", "region_id", "workspace_id", "assigned_at", "pool_state", "user_id",
)
_CHANNEL_IDENTITY_FIELDS = (
    *_ASSIGNMENT_FIELDS,
    "channel_kind", "private_ip", "tunnel_bind", "tunnel_port",
    "tunnel_fingerprint", "tunnel_pubkey", "action_api_key_hash", "action_api_key_ciphertext",
    "channel_attempt_id", "channel_enrollment_grant",
)


def _snapshot_conditions(expected: dict, names=_CHANNEL_IDENTITY_FIELDS):
    if expected.get("is_deleted") or any(name not in expected for name in names):
        return None
    return (*(getattr(CloudDesktop, name) == expected[name] for name in names),
            CloudDesktop.is_deleted.is_(False))


def _channel_conditions(expected: dict, *, same_state: bool = True):
    state = expected.get("tunnel_state")
    if (state not in ("pending", "up", "down") or expected.get("is_deleted")
            or any(name not in expected for name in _CHANNEL_IDENTITY_FIELDS)):
        return None
    return (
        *(getattr(CloudDesktop, name) == expected[name] for name in _CHANNEL_IDENTITY_FIELDS),
        CloudDesktop.is_deleted.is_(False),
        (CloudDesktop.tunnel_state == state if same_state
         else CloudDesktop.tunnel_state.in_(("pending", "up", "down"))),
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
        session=None,
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
        if session is not None:
            session.add(row)
            await session.flush()
        else:
            async with get_db_session() as owned:
                owned.add(row)
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
        *, usable_until: datetime | None = None, session=None,
    ) -> dict | None:
        """Atomically claim prewarm capacity, optionally inside caller authority locks."""
        async with self._pool_lock:
            if session is not None:
                return await self._claim_prewarm_locked(session, workspace_id, triggered_by_user_id, usable_until)
            async with get_db_session() as owned:
                return await self._claim_prewarm_locked(owned, workspace_id, triggered_by_user_id, usable_until)

    async def _claim_prewarm_locked(self, session, workspace_id, triggered_by_user_id, usable_until):
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
        row.assigned_at = now
        row.channel_enrollment_grant = ascending("cgrant")
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

    async def claim_channel_attempt(self, expected: dict) -> dict | None:
        """Claim the original snapshot; consume only a real pool claim grant.

        A retry may replace an unfinished *live* attempt. Explicit revocation
        is durable: no automatic recovery may acquire authority from it.
        """
        conditions = _snapshot_conditions(expected)
        state = expected.get("tunnel_state")
        enrollment = (state == "revoked" and expected.get("channel_enrollment_grant")
                      and expected.get("pool_state") == "assigning" and expected.get("workspace_id"))
        if conditions is None or (state not in ("pending", "up", "down") and not enrollment):
            return None
        values = {"channel_attempt_id": ascending("chattempt"), "channel_enrollment_grant": None,
                  "updated_at": datetime.now(timezone.utc)}
        if enrollment:
            values.update(tunnel_state="pending", channel_error=None)
        async with get_db_session() as db:
            row = await db.scalar(update(CloudDesktop).where(*conditions,
                CloudDesktop.tunnel_state == state).values(**values).returning(CloudDesktop)
                .execution_options(synchronize_session=False))
            return _to_dict(row) if row else None

    async def channel_attempt_current(self, expected: dict, *, states=("pending", "up", "down")) -> bool:
        conditions = _snapshot_conditions(expected)
        if conditions is None or not expected.get("channel_attempt_id"):
            return False
        async with get_db_session() as db:
            return await db.scalar(select(CloudDesktop.id).where(*conditions,
                CloudDesktop.tunnel_state.in_(states))) is not None

    async def write_channel_attempt(
        self, expected: dict, fields: dict, *, states=("pending", "up", "down"), session=None,
    ) -> dict | None:
        """Update and return only this attempt, in the caller's transaction if supplied."""
        conditions = _snapshot_conditions(expected)
        if conditions is None or not expected.get("channel_attempt_id"):
            return None
        if {"id", "region_id", "channel_attempt_id", "channel_enrollment_grant"} & fields.keys():
            raise ValueError("attempt writes cannot replace their own authority")
        statement = update(CloudDesktop).where(*conditions, CloudDesktop.tunnel_state.in_(states)).values(
            **fields, updated_at=datetime.now(timezone.utc)).returning(CloudDesktop).execution_options(
                synchronize_session=False)
        if session is not None:
            row = await session.scalar(statement)
            return _to_dict(row) if row else None
        async with get_db_session() as db:
            row = await db.scalar(statement)
            return _to_dict(row) if row else None

    async def reserve_attempt_port(self, expected: dict, low: int, high: int) -> dict | None:
        if not 1 <= low <= high <= 65535:
            raise ValueError("invalid WUYING_TUNNEL_PORT_RANGE")
        conditions = _snapshot_conditions(expected)
        if conditions is None or not expected.get("channel_attempt_id"):
            return None
        async with self._port_lock:
            for _attempt in range(high - low + 1):
                try:
                    async with get_db_session() as db:
                        row = await db.scalar(select(CloudDesktop).where(*conditions,
                            CloudDesktop.tunnel_state.in_(("pending", "up", "down"))).with_for_update())
                        if row is None:
                            return None
                        if row.tunnel_port is None:
                            used = set(await db.scalars(select(CloudDesktop.tunnel_port).where(
                                CloudDesktop.tunnel_port.is_not(None))))
                            port = next((value for value in range(low, high + 1) if value not in used), None)
                            if port is None:
                                raise RuntimeError("WUYING tunnel port range exhausted")
                            row.tunnel_port = port
                            row.updated_at = datetime.now(timezone.utc)
                            await db.flush()
                        return _to_dict(row)
                except IntegrityError:
                    continue
        raise RuntimeError("could not reserve a WUYING tunnel port after concurrent conflicts")

    async def revoke_channel_assignment(self, expected: dict) -> dict | None:
        """Revoke the named assignment, including an install currently changing its keys."""
        conditions = _snapshot_conditions(expected, _ASSIGNMENT_FIELDS)
        if conditions is None:
            return None
        async with get_db_session() as db:
            row = await db.scalar(update(CloudDesktop).where(*conditions).values(
                tunnel_state="revoked", channel_attempt_id=ascending("chattempt"),
                channel_enrollment_grant=None, updated_at=datetime.now(timezone.utc)
            ).returning(CloudDesktop).execution_options(synchronize_session=False))
            return _to_dict(row) if row else None

    async def record_channel_probe(self, expected: dict, *, healthy: bool, error: str | None = None) -> bool:
        """Commit probe health only if its original live channel still exists.

        No transaction spans the network probe. A single conditional UPDATE
        arbitrates with revoke/reassignment, including a concurrent uncommitted
        change, without a read-then-write authorization window.
        """
        state = expected.get("tunnel_state")
        if not healthy and state != "up":
            return False
        now = datetime.now(timezone.utc)
        fields = {"tunnel_state": "up" if healthy else "down", "updated_at": now,
                  "channel_error": None if healthy else error}
        if healthy:
            fields["last_seen_at"] = now
        return await self._update_channel_snapshot(expected, fields)

    async def channel_binding_current(self, expected: dict) -> bool:
        """Check the original snapshot in a short read without locking a row."""
        conditions = _channel_conditions(expected)
        if conditions is None:
            return False
        async with get_db_session() as session:
            return await session.scalar(select(CloudDesktop.id).where(*conditions)) is not None

    async def record_channel_verification(
        self, expected: dict, *, state: str | None = None, error: str | None = None,
        seen_at: datetime | None = None,
    ) -> bool:
        """Only update verification health for its still-current binding."""
        if state not in (None, "up", "down"):
            raise ValueError("invalid channel verification state")
        fields = {"channel_error": error}
        if state is not None:
            fields["tunnel_state"] = state
        if state == "up":
            fields["last_seen_at"] = seen_at or datetime.now(timezone.utc)
        return await self._update_channel_snapshot(expected, fields)

    async def record_channel_recovery_failure(self, expected: dict, error: str) -> bool:
        # verify may already have recorded down before returning its error.
        # Never turn a concurrent revoke/new binding into a retryable failure.
        return await self._update_channel_snapshot(expected, {
            "status": "starting", "error": error, "tunnel_state": "down", "channel_error": error,
        }, same_state=False)

    async def _update_channel_snapshot(self, expected: dict, fields: dict, *, same_state: bool = True) -> bool:
        conditions = _channel_conditions(expected, same_state=same_state)
        if conditions is None:
            return False
        fields = {**fields, "updated_at": datetime.now(timezone.utc)}
        async with get_db_session() as session:
            result = await session.execute(
                update(CloudDesktop).where(*conditions).values(**fields)
                .execution_options(synchronize_session=False)
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
