"""PostgreSQL implementation of IPreferenceRepo."""
from sqlalchemy import select

from core.identifier import generate_id
from db.base import get_db_session
from db.models.preference import UserPreference


class PgPreferenceRepo:
    async def get(self, user_id: str) -> dict | None:
        async with get_db_session() as session:
            result = await session.execute(
                select(UserPreference).where(UserPreference.user_id == user_id)
            )
            row = result.scalar_one_or_none()
            return _to_dict(row) if row else None

    async def upsert(self, user_id: str, **fields) -> dict:
        async with get_db_session() as session:
            result = await session.execute(
                select(UserPreference).where(UserPreference.user_id == user_id)
            )
            row = result.scalar_one_or_none()
            fields = dict(fields)
            onboarding = fields.pop("onboarding", None)
            if row:
                extra_patch = fields.pop("extra", None)
                for k, v in fields.items():
                    setattr(row, k, v)
                if extra_patch is not None or onboarding is not None:
                    row.extra = _merged_extra(row.extra, extra_patch, onboarding)
            else:
                fields["extra"] = _merged_extra({}, fields.get("extra"), onboarding)
                row = UserPreference(id=generate_id(), user_id=user_id, **fields)
                session.add(row)
            return _to_dict(row)


ONBOARDING_KEY = "onboarding"


def _merged_extra(current: dict | None, patch: dict | None, onboarding: dict | None) -> dict:
    """Shallow-merge ``extra`` so one client's keys never wipe another's.

    The web appearance page writes ``{mode, fontSize, locale}``, the mobile
    app writes ``onboarding``, the browser preference writes ``browser_mode``;
    each used to replace the whole column. ``onboarding`` replaces its own
    value wholesale (the client owns the full map), so ``{}`` clears it.
    """
    merged = dict(current or {})
    if patch:
        merged.update(patch)
    if onboarding is not None:
        merged[ONBOARDING_KEY] = dict(onboarding)
    return merged


def _to_dict(row: UserPreference) -> dict:
    data = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    extra = data.get("extra") or {}
    data[ONBOARDING_KEY] = dict(extra.get(ONBOARDING_KEY) or {}) if isinstance(extra, dict) else {}
    return data
