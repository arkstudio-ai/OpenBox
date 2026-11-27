"""Populate missing phones from a trusted Logto Management API user export.

Run from backend after the phone migration. The file must contain a JSON
array of Logto profiles (id, primaryPhone). Preview by default, or use
--apply to update missing values. Existing values win over the export.
"""
import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select, update

from auth.contact import normalize_phone
from auth.logto import PROVIDER
from core.config import get_config
from db.base import close_engine, get_db_session, init_engine
from db.models.user import User


async def backfill(profiles: list[dict], *, apply: bool) -> int:
    phones = {
        profile["id"]: phone
        for profile in profiles
        if isinstance(profile.get("id"), str)
        and (phone := normalize_phone(profile.get("primaryPhone")))
    }
    if not phones:
        return 0
    matched = 0
    async with get_db_session() as db:
        subjects = list(phones)
        for offset in range(0, len(subjects), 500):
            query = select(User.id, User.oauth_id).where(
                User.oauth_provider == PROVIDER,
                User.oauth_id.in_(subjects[offset:offset + 500]),
                User.phone_number.is_(None),
                User.is_deleted.is_(False),
            )
            rows = (await db.execute(query)).all()
            for user_id, subject in rows:
                if apply:
                    result = await db.execute(update(User).where(
                        User.id == user_id, User.phone_number.is_(None), User.is_deleted.is_(False),
                    ).values(phone_number=phones[subject]))
                    matched += result.rowcount
                else:
                    matched += 1
    return matched


async def _main(file: Path, apply: bool) -> None:
    profiles = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(profiles, list) or any(not isinstance(item, dict) for item in profiles):
        raise ValueError("Expected a JSON array of Logto profiles")
    init_engine(get_config().database_url)
    try:
        count = await backfill(profiles, apply=apply)
        print(f"Accounts {'updated' if apply else 'eligible'}: {count}")
    finally:
        await close_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(_main(args.file, args.apply))
