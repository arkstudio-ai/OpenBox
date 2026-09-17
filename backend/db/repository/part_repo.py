"""Read-only public Part projection queries.

Part mutations are canonical Agent transcript commands and must go through
``session.session`` so the SQL read model, run fence, and ``agent_events`` are
committed atomically.  Generic create/upsert/update methods are deliberately
absent to make that boundary structural instead of conventional.
"""

from sqlalchemy import select

from db.base import get_db_session
from db.models.part import Part, PRIVATE_TOOL_PART_FIELDS, public_part_data


class PgPartRepo:
    async def get(self, part_id: str) -> dict | None:
        async with get_db_session() as session:
            result = await session.execute(select(Part).where(Part.id == part_id))
            row = result.scalar_one_or_none()
            return _to_dict(row) if row else None

    async def list_by_message(self, message_id: str) -> list[dict]:
        async with get_db_session() as session:
            result = await session.execute(
                select(Part).where(Part.message_id == message_id)
                .order_by(Part.created_at)
            )
            return [_to_dict(r) for r in result.scalars().all()]

def _to_dict(row: Part) -> dict:
    d = {}
    for c in row.__table__.columns:
        if c.name in PRIVATE_TOOL_PART_FIELDS:
            continue
        v = getattr(row, c.name)
        if c.name == "data" and isinstance(v, dict):
            # Merge data fields into top level for compatibility with existing code
            v = public_part_data(v)
            d.update(v)
        d[c.name] = v
    return d
