"""Durable ownership and publication state for user-created skills.

The executable copy of a personal skill lives in the user's sandbox.  This
module stores a bounded snapshot of that copy so ownership, publication and
store-install provenance survive sandbox restarts.  It deliberately does not
load skill instructions into an agent context; discovery/loading remains the
responsibility of :mod:`skill.skill` and happens only when a skill is used.

Every public function returns ordinary dictionaries.  Archive bytes are
excluded unless a trusted download/install path explicitly asks for them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.identifier import ascending
from db.base import get_db_session
from db.models.skill_install import SkillInstall
from db.models.user import User
from db.models.user_skill import UserSkill


COMMUNITY_PREFIX = "community:"
UNPUBLISHED = "unpublished"
PUBLISHED = "published"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    # SQLite drops timezone information even for DateTime(timezone=True).
    # Stored timestamps are UTC, so restore that fact and keep API output
    # stable across SQLite tests and PostgreSQL production.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _required_text(value: object, field: str, *, limit: int = 64) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    result = value.strip()
    if len(result) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    if "/" in result or "\\" in result or result in {".", ".."}:
        raise ValueError(f"{field} must be a single directory-safe name")
    return result


def _optional_text(value: object, *, limit: int | None = None) -> str:
    result = value if isinstance(value, str) else ""
    if limit is not None and len(result) > limit:
        # Icons are display decoration, not package identity.  A malformed
        # oversized value must not make an otherwise valid snapshot fail.
        return result[:limit]
    return result


def _string_list(value: object, *, limit: int = 128) -> list[str]:
    if isinstance(value, str):
        values: Sequence[object] = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        return []
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if item and item not in result:
            result.append(item)
        if len(result) >= limit:
            break
    return result


def _snapshot_metadata(skill_info: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only small catalogue/listing metadata, never instructions/paths."""
    return {
        "homepage": _optional_text(skill_info.get("homepage"), limit=2048),
        "requires_mcp": _string_list(
            skill_info.get("requires_mcp", skill_info.get("requires-mcp"))
        ),
        "files": _string_list(skill_info.get("files"), limit=256),
        "tags": _string_list(skill_info.get("tags"), limit=32),
    }


def _community_id(row_id: str) -> str:
    return f"{COMMUNITY_PREFIX}{row_id}"


def _row_id(identifier: str) -> str:
    if identifier.startswith(COMMUNITY_PREFIX):
        return identifier[len(COMMUNITY_PREFIX) :]
    return identifier


def _has_published_snapshot(row: UserSkill) -> bool:
    """Return whether a complete public release is available for consumers."""
    return row.status == PUBLISHED and row.published_archive_data is not None


def _has_unpublished_changes(row: UserSkill) -> bool:
    """Compare the current draft with the immutable public release."""
    if not _has_published_snapshot(row):
        return True
    return (
        row.name != row.published_name
        or row.install_dir != row.published_install_dir
        or row.description != row.published_description
        or row.icon != row.published_icon
        or row.archive_sha256 != row.published_archive_sha256
        or row.archive_size != row.published_archive_size
        or (row.metadata_data or {}) != (row.published_metadata_data or {})
    )


def _snapshot_dict(row: UserSkill, *, include_archive: bool = False) -> dict[str, Any]:
    metadata = row.metadata_data or {}
    published = _has_published_snapshot(row)
    publication_status = PUBLISHED if published else UNPUBLISHED
    result: dict[str, Any] = {
        "id": row.id,
        "library_id": row.id,
        "catalog_id": _community_id(row.id) if published else None,
        "name": row.name,
        "title": row.name,
        "install_dir": row.install_dir,
        "description": row.description,
        "icon": row.icon,
        "category": "personal",
        "publication_status": publication_status,
        "status": publication_status,
        "version": row.version,
        "draft_version": row.version,
        "published_version": row.published_version if published else None,
        "archive_size": row.archive_size,
        "published_archive_size": row.published_archive_size if published else None,
        "has_unpublished_changes": _has_unpublished_changes(row),
        "restore_available": bool(row.archive_data),
        "homepage": metadata.get("homepage", ""),
        "requires_mcp": list(metadata.get("requires_mcp") or []),
        "files": list(metadata.get("files") or []),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "published_at": _iso(row.published_at),
    }
    if include_archive:
        result["archive_data"] = row.archive_data
        result["archive_sha256"] = row.archive_sha256
    return result


def _published_snapshot_dict(
    row: UserSkill,
    *,
    include_archive: bool = False,
) -> dict[str, Any]:
    """Serialize only the public release, never mutable draft fields."""
    metadata = row.published_metadata_data or {}
    result: dict[str, Any] = {
        "id": row.id,
        "library_id": row.id,
        "catalog_id": _community_id(row.id),
        "name": row.published_name,
        "title": row.published_name,
        "install_dir": row.published_install_dir,
        "description": row.published_description or "",
        "icon": row.published_icon or "",
        # Keep the historical return shape used by the metadata installer;
        # provenance changes to "store" only after installation succeeds.
        "category": "personal",
        "publication_status": PUBLISHED,
        "status": PUBLISHED,
        "version": row.published_version,
        "archive_size": row.published_archive_size,
        "homepage": metadata.get("homepage", ""),
        "requires_mcp": list(metadata.get("requires_mcp") or []),
        "files": list(metadata.get("files") or []),
        "created_at": _iso(row.created_at),
        # A draft refresh must not make a public release look newer.
        "updated_at": _iso(row.published_at),
        "published_at": _iso(row.published_at),
    }
    if include_archive:
        result["archive_data"] = row.published_archive_data
        result["archive_sha256"] = row.published_archive_sha256
    return result


async def _owned_row(
    session: AsyncSession,
    user_id: str,
    identifier: str,
    *,
    workspace_id: str | None = None,
    for_update: bool = False,
) -> UserSkill | None:
    """Resolve an owned row deterministically by id, then name, then directory."""
    raw = _row_id(identifier)
    for column, value in (
        (UserSkill.id, raw),
        (UserSkill.name, identifier),
        (UserSkill.install_dir, identifier),
    ):
        statement = (
            select(UserSkill)
            .where(
                UserSkill.owner_id == user_id,
                column == value,
                *([UserSkill.workspace_id == workspace_id] if workspace_id else []),
            )
            .order_by(UserSkill.updated_at.desc(), UserSkill.id.desc())
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await session.execute(statement)).scalar_one_or_none()
        if row is not None:
            return row
    return None


async def _resolve_workspace_id(user_id: str, workspace_id: str | None) -> str:
    """Keep direct/internal callers compatible while HTTP callers pass selection."""
    if workspace_id:
        return workspace_id
    async with get_db_session() as session:
        resolved = (
            await session.execute(
                select(User.default_workspace_id).where(User.id == user_id)
            )
        ).scalar_one_or_none()
    return resolved or "ws_default"


def _apply_snapshot(
    row: UserSkill,
    *,
    install_dir: str,
    description: str,
    icon: str,
    archive_data: bytes,
    archive_sha256: str,
    metadata: dict[str, Any],
    now: datetime,
) -> None:
    had_published_snapshot = _has_published_snapshot(row)
    changed = (
        row.install_dir != install_dir
        or row.description != description
        or row.icon != icon
        or row.archive_sha256 != archive_sha256
        or row.archive_size != len(archive_data)
        or (row.metadata_data or {}) != metadata
    )
    if not changed:
        return

    row.install_dir = install_dir
    row.description = description
    row.icon = icon
    row.archive_data = archive_data
    row.archive_sha256 = archive_sha256
    row.archive_size = len(archive_data)
    row.metadata_data = metadata
    row.version += 1
    # Exporting or downloading a changed draft must not withdraw or mutate a
    # release that users may already be installing.  Only explicit publish
    # switches the public snapshot.
    row.status = PUBLISHED if had_published_snapshot else UNPUBLISHED
    row.updated_at = now


async def upsert_personal_snapshot(
    user_id: str,
    skill_info: Mapping[str, Any],
    archive_data: bytes,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Create or refresh one user's durable personal-skill snapshot.

    The owner/name pair is the stable identity.  Re-saving byte-for-byte
    identical content is idempotent.  Changes increment the draft version but
    leave any existing public release untouched until an explicit publish.
    """
    user_id = _required_text(user_id, "user_id")
    workspace_id = await _resolve_workspace_id(user_id, workspace_id)
    if not isinstance(skill_info, Mapping):
        raise ValueError("skill_info must be an object")
    if not isinstance(archive_data, (bytes, bytearray, memoryview)):
        raise ValueError("archive_data must be bytes")
    archive = bytes(archive_data)
    if not archive:
        raise ValueError("archive_data cannot be empty")

    name = _required_text(skill_info.get("name"), "name")
    install_dir = _required_text(skill_info.get("install_dir") or name, "install_dir")
    description = _optional_text(skill_info.get("description"))
    icon = _optional_text(skill_info.get("icon"), limit=16)
    digest = sha256(archive).hexdigest()
    metadata = _snapshot_metadata(skill_info)
    now = _now()

    async def save(*, retry: bool) -> dict[str, Any]:
        async with get_db_session() as session:
            statement = select(UserSkill).where(
                UserSkill.owner_id == user_id,
                UserSkill.workspace_id == workspace_id,
                UserSkill.name == name,
            )
            if retry:
                statement = statement.with_for_update()
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                row = UserSkill(
                    id=ascending("skill"),
                    owner_id=user_id,
                    workspace_id=workspace_id,
                    name=name,
                    install_dir=install_dir,
                    description=description,
                    icon=icon,
                    status=UNPUBLISHED,
                    version=1,
                    archive_data=archive,
                    archive_sha256=digest,
                    archive_size=len(archive),
                    metadata_data=metadata,
                    published_name=None,
                    published_install_dir=None,
                    published_description=None,
                    published_icon=None,
                    published_version=None,
                    published_archive_data=None,
                    published_archive_sha256=None,
                    published_archive_size=None,
                    published_metadata_data=None,
                    created_at=now,
                    updated_at=now,
                    published_at=None,
                )
                session.add(row)
            else:
                _apply_snapshot(
                    row,
                    install_dir=install_dir,
                    description=description,
                    icon=icon,
                    archive_data=archive,
                    archive_sha256=digest,
                    metadata=metadata,
                    now=now,
                )
            # Flush here so a concurrent owner/name insert is caught inside the
            # retry boundary rather than by the context manager's commit.
            await session.flush()
            return _snapshot_dict(row)

    try:
        return await save(retry=False)
    except IntegrityError:
        # The database unique constraint is the final arbiter if two chat turns
        # snapshot the same newly-created skill at once.
        return await save(retry=True)


async def get_owned_skill(
    user_id: str,
    identifier: str,
    *,
    workspace_id: str | None = None,
    include_archive: bool = False,
) -> dict[str, Any] | None:
    """Get a skill by id, name or install directory, enforcing ownership."""
    user_id = _required_text(user_id, "user_id")
    identifier = _required_text(identifier, "identifier")
    async with get_db_session() as session:
        row = await _owned_row(session, user_id, identifier, workspace_id=workspace_id)
        return _snapshot_dict(row, include_archive=include_archive) if row else None


async def list_owned_skills(
    user_id: str, workspace_id: str | None = None
) -> list[dict[str, Any]]:
    """List a user's durable draft snapshots using a JSON-stable contract.

    Archive bytes and hashes are intentionally absent.  A restore path first
    discovers entries here, then fetches one owner-only snapshot through
    :func:`get_owned_skill` with ``include_archive=True``.
    """
    user_id = _required_text(user_id, "user_id")
    async with get_db_session() as session:
        rows = list(
            (
                await session.execute(
                    select(UserSkill)
                    .where(
                        UserSkill.owner_id == user_id,
                        *([UserSkill.workspace_id == workspace_id] if workspace_id else []),
                    )
                    .order_by(UserSkill.updated_at.desc(), UserSkill.id.desc())
                )
            ).scalars()
        )
    return [_snapshot_dict(row) for row in rows]


async def publish_personal_skill(
    user_id: str, identifier: str, workspace_id: str | None = None
) -> dict[str, Any]:
    """Atomically copy the owner's current draft into the public release."""
    user_id = _required_text(user_id, "user_id")
    identifier = _required_text(identifier, "identifier")
    async with get_db_session() as session:
        row = await _owned_row(
            session, user_id, identifier,
            workspace_id=workspace_id, for_update=True,
        )
        if row is None:
            raise LookupError("Personal skill not found")
        if _has_unpublished_changes(row):
            now = _now()
            row.published_name = row.name
            row.published_install_dir = row.install_dir
            row.published_description = row.description
            row.published_icon = row.icon
            row.published_version = (row.published_version or 0) + 1
            row.published_archive_data = row.archive_data
            row.published_archive_sha256 = row.archive_sha256
            row.published_archive_size = row.archive_size
            row.published_metadata_data = dict(row.metadata_data or {})
            row.status = PUBLISHED
            row.published_at = now
            row.updated_at = now
            await session.flush()
        return _snapshot_dict(row)


async def get_published_skill(
    identifier: str,
    *,
    include_archive: bool = False,
) -> dict[str, Any] | None:
    """Resolve a public snapshot by its opaque ``community:<id>`` identifier."""
    identifier = _required_text(identifier, "identifier")
    row_id = _row_id(identifier)
    async with get_db_session() as session:
        row = (
            await session.execute(
                select(UserSkill)
                .join(User, User.id == UserSkill.owner_id)
                .where(
                    UserSkill.id == row_id,
                    UserSkill.status == PUBLISHED,
                    UserSkill.published_archive_data.is_not(None),
                    User.is_active.is_(True),
                    User.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        return (
            _published_snapshot_dict(row, include_archive=include_archive)
            if row
            else None
        )


async def list_published_catalog_entries() -> list[dict[str, Any]]:
    """Return public, JSON-safe catalogue entries with no archive material."""
    async with get_db_session() as session:
        rows = (
            await session.execute(
                select(UserSkill, User.username)
                .join(User, User.id == UserSkill.owner_id)
                .where(
                    UserSkill.status == PUBLISHED,
                    UserSkill.published_archive_data.is_not(None),
                    User.is_active.is_(True),
                    User.is_deleted.is_(False),
                )
                .order_by(
                    UserSkill.published_at.desc(),
                    UserSkill.published_name.asc(),
                )
            )
        ).all()

    entries: list[dict[str, Any]] = []
    for row, publisher in rows:
        metadata = row.published_metadata_data or {}
        requirements = list(metadata.get("requires_mcp") or [])
        tags = list(metadata.get("tags") or [])
        if "community" not in tags:
            tags.append("community")
        entries.append(
            {
                "id": _community_id(row.id),
                "kind": "skill",
                "name": row.published_name,
                "title": row.published_name,
                "icon": row.published_icon or "",
                "description": row.published_description or "",
                "publisher": publisher,
                "homepage": metadata.get("homepage", ""),
                "tags": tags,
                "requires_mcp": requirements,
                "missing_mcp": requirements.copy(),
                "install": {},
                "installed": False,
                "community": True,
                "version": row.published_version,
                "published_at": _iso(row.published_at),
            }
        )
    return entries


async def annotate_installed_skills(
    user_id: str,
    installed_skills: Sequence[Mapping[str, Any]],
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Attach durable product provenance to a sandbox/host skill listing."""
    user_id = _required_text(user_id, "user_id")
    async with get_db_session() as session:
        personal = list(
            (
                await session.execute(
                    select(UserSkill).where(
                        UserSkill.owner_id == user_id,
                        *([UserSkill.workspace_id == workspace_id] if workspace_id else []),
                    )
                )
            ).scalars()
        )
        store_installs = list(
            (
                await session.execute(
                    select(SkillInstall).where(SkillInstall.user_id == user_id)
                )
            ).scalars()
        )

    personal_by_dir = {row.install_dir: row for row in personal}
    personal_by_name = {row.name: row for row in personal}
    store_by_dir = {row.install_dir: row for row in store_installs}
    store_by_name = {row.name: row for row in store_installs}

    annotated: list[dict[str, Any]] = []
    for raw_skill in installed_skills:
        skill = dict(raw_skill)
        source = skill.get("source")
        name = skill.get("name") if isinstance(skill.get("name"), str) else ""
        install_dir = (
            skill.get("install_dir") if isinstance(skill.get("install_dir"), str) else ""
        )

        if source == "builtin":
            category = "builtin"
            personal_row = None
            store_row = None
        elif source != "container":
            category = "host"
            personal_row = None
            store_row = None
        else:
            # Exact install-directory provenance wins.  Falling back to the
            # display name is only needed for older scanners with no directory.
            personal_row = (
                personal_by_dir.get(install_dir)
                if install_dir
                else personal_by_name.get(name)
            )
            store_row = (
                store_by_dir.get(install_dir)
                if install_dir
                else store_by_name.get(name)
            )
            # A recorded store install is the strongest evidence for the live
            # directory.  This matters after an author uninstalls a personal
            # copy but keeps its durable/public snapshot, then installs a
            # community package using the same slug: the old ownership record
            # must not relabel the new filesystem copy as personal.
            if store_row is not None:
                category = "store"
            elif personal_row is not None:
                category = "personal"
            else:
                # A filesystem copy is not proof of authorship.  Older/manual
                # installs have no durable provenance, so keep them distinct
                # instead of offering another person's skill as "personal".
                category = "installed"

        skill["category"] = category
        if category == "personal":
            is_published = bool(
                personal_row is not None and _has_published_snapshot(personal_row)
            )
            skill["publication_status"] = PUBLISHED if is_published else UNPUBLISHED
            skill["library_id"] = personal_row.id if personal_row is not None else None
            skill["catalog_id"] = (
                _community_id(personal_row.id)
                if is_published
                else None
            )
            skill["published_at"] = (
                _iso(personal_row.published_at) if personal_row is not None else None
            )
        elif category == "store":
            skill["publication_status"] = None
            skill["library_id"] = None
            skill["catalog_id"] = _community_id(store_row.user_skill_id)
            skill["published_at"] = None
        else:
            skill["publication_status"] = None
            skill["library_id"] = None
            skill["catalog_id"] = None
            skill["published_at"] = None
        annotated.append(skill)

    return annotated


async def record_community_installation(
    *,
    user_id: str,
    user_skill_id: str,
    name: str,
    install_dir: str,
) -> dict[str, Any]:
    """Upsert provenance after a published community ZIP installs successfully."""
    user_id = _required_text(user_id, "user_id")
    user_skill_id = _required_text(_row_id(user_skill_id), "user_skill_id")
    name = _required_text(name, "name")
    install_dir = _required_text(install_dir, "install_dir")
    now = _now()

    async with get_db_session() as session:
        published = (
            await session.execute(
                select(UserSkill.id)
                .join(User, User.id == UserSkill.owner_id)
                .where(
                    UserSkill.id == user_skill_id,
                    UserSkill.status == PUBLISHED,
                    UserSkill.published_archive_data.is_not(None),
                    User.is_active.is_(True),
                    User.is_deleted.is_(False),
                )
                .with_for_update(of=UserSkill)
            )
        ).scalar_one_or_none()
        if published is None:
            raise LookupError("Published skill not found")

        row = (
            await session.execute(
                select(SkillInstall)
                .where(
                    SkillInstall.user_id == user_id,
                    SkillInstall.install_dir == install_dir,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            row = SkillInstall(
                id=ascending("skillinstall"),
                user_id=user_id,
                user_skill_id=user_skill_id,
                name=name,
                install_dir=install_dir,
                installed_at=now,
            )
            session.add(row)
        else:
            row.user_skill_id = user_skill_id
            row.name = name
            row.installed_at = now
        await session.flush()
        return {
            "id": row.id,
            "name": row.name,
            "install_dir": row.install_dir,
            "category": "store",
            "publication_status": None,
            "library_id": None,
            "catalog_id": _community_id(row.user_skill_id),
            "installed_at": _iso(row.installed_at),
        }


async def remove_community_installation(user_id: str, identifier: str) -> bool:
    """Remove only this user's provenance row after its sandbox uninstall."""
    user_id = _required_text(user_id, "user_id")
    identifier = _required_text(identifier, "identifier")
    row_id = _row_id(identifier)
    async with get_db_session() as session:
        result = await session.execute(
            delete(SkillInstall).where(
                SkillInstall.user_id == user_id,
                (
                    (SkillInstall.install_dir == identifier)
                    | (SkillInstall.name == identifier)
                    | (SkillInstall.user_skill_id == row_id)
                ),
            )
        )
        return bool(result.rowcount)


async def delete_owned_skill(
    user_id: str, identifier: str, workspace_id: str | None = None
) -> bool:
    """Delete one owned library snapshot and all installation provenance.

    Resolution is owner-scoped before either DELETE runs, so another user's
    private or public skill can never be removed by guessing its identifier.
    """
    user_id = _required_text(user_id, "user_id")
    identifier = _required_text(identifier, "identifier")
    async with get_db_session() as session:
        row = await _owned_row(
            session, user_id, identifier,
            workspace_id=workspace_id, for_update=True,
        )
        if row is None:
            return False
        await session.execute(
            delete(SkillInstall).where(SkillInstall.user_skill_id == row.id)
        )
        result = await session.execute(
            delete(UserSkill).where(
                UserSkill.id == row.id,
                UserSkill.owner_id == user_id,
                *([UserSkill.workspace_id == workspace_id] if workspace_id else []),
            )
        )
        return bool(result.rowcount)
