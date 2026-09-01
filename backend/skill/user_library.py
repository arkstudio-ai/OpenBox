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
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.skill_install import SkillInstall
from db.models.user import User
from db.models.user_skill import UserSkill
from sandbox.client import (
    SkillArchiveAlreadyExistsError,
    SkillRestoreFencedError,
)


COMMUNITY_PREFIX = "community:"
UNPUBLISHED = "unpublished"
PUBLISHED = "published"
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_DELETING = "deleting"
LIFECYCLE_DELETED = "deleted"

log = create_logger("skill.user_library")


class SkillRestoreScopeError(RuntimeError):
    """The sandbox tenant scope does not match the database owner."""


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
        "lifecycle_generation": row.lifecycle_generation,
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
    for_update: bool = False,
    include_inactive: bool = False,
) -> UserSkill | None:
    """Resolve an owned row deterministically by id, then name, then directory."""
    raw = _row_id(identifier)
    for column, value in (
        (UserSkill.id, raw),
        (UserSkill.name, identifier),
        (UserSkill.install_dir, identifier),
    ):
        predicates = [UserSkill.owner_id == user_id, column == value]
        if not include_inactive:
            predicates.append(UserSkill.lifecycle_state == LIFECYCLE_ACTIVE)
        statement = (
            select(UserSkill)
            .where(*predicates)
            .order_by(UserSkill.updated_at.desc(), UserSkill.id.desc())
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await session.execute(statement)).scalar_one_or_none()
        if row is not None:
            return row
    return None


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


def _reactivate_tombstone(row: UserSkill, now: datetime) -> None:
    """Start a new lifecycle generation without reviving an old publication."""
    if row.lifecycle_state == LIFECYCLE_ACTIVE:
        return
    row.lifecycle_state = LIFECYCLE_ACTIVE
    row.lifecycle_generation += 1
    row.status = UNPUBLISHED
    row.published_name = None
    row.published_install_dir = None
    row.published_description = None
    row.published_icon = None
    row.published_version = None
    row.published_archive_data = None
    row.published_archive_sha256 = None
    row.published_archive_size = None
    row.published_metadata_data = None
    row.published_at = None
    row.updated_at = now


async def upsert_personal_snapshot(
    user_id: str,
    skill_info: Mapping[str, Any],
    archive_data: bytes,
) -> dict[str, Any]:
    """Create or refresh one user's durable personal-skill snapshot.

    The owner/name pair is the stable identity.  Re-saving byte-for-byte
    identical content is idempotent.  Changes increment the draft version but
    leave any existing public release untouched until an explicit publish.
    """
    user_id = _required_text(user_id, "user_id")
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
                UserSkill.name == name,
            )
            # Serialize reactivation against a concurrent durable uninstall.
            # PostgreSQL supplies the row lock; the Action Server generation
            # fence remains the cross-system arbiter for SQLite and crashes.
            statement = statement.with_for_update()
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                row = UserSkill(
                    id=ascending("skill"),
                    owner_id=user_id,
                    name=name,
                    install_dir=install_dir,
                    description=description,
                    icon=icon,
                    status=UNPUBLISHED,
                    lifecycle_state=LIFECYCLE_ACTIVE,
                    lifecycle_generation=1,
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
                _reactivate_tombstone(row, now)
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
    include_archive: bool = False,
) -> dict[str, Any] | None:
    """Get a skill by id, name or install directory, enforcing ownership."""
    user_id = _required_text(user_id, "user_id")
    identifier = _required_text(identifier, "identifier")
    async with get_db_session() as session:
        row = await _owned_row(session, user_id, identifier)
        return _snapshot_dict(row, include_archive=include_archive) if row else None


async def list_owned_skills(user_id: str) -> list[dict[str, Any]]:
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
                        UserSkill.lifecycle_state == LIFECYCLE_ACTIVE,
                    )
                    .order_by(UserSkill.updated_at.desc(), UserSkill.id.desc())
                )
            ).scalars()
        )
    return [_snapshot_dict(row) for row in rows]


async def restore_personal_skills_to_sandbox(
    user_id: str,
    sandbox,
    *,
    owned_skills: Sequence[Mapping[str, Any]] | None = None,
    installed_skills: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Restore missing owner snapshots before a sandbox catalogue is consumed.

    Both the Agent path and Skill Center call this helper. Archive lookup is
    owner-filtered again for every upload; caller-provided metadata is only a
    hint identifying which immutable database row to fetch. A live package
    with the same name or directory is never overwritten.

    Restoration is best effort at the caller boundary, but corruption and a
    mismatched tenant-scoped sandbox are hard failures here so neither can turn
    into a cross-user upload.
    """
    user_id = _required_text(user_id, "user_id")
    if sandbox is None:
        return [dict(item) for item in (installed_skills or [])]

    # Real WUYING clients carry the pseudonymous filesystem scope derived by
    # the backend. Test/legacy clients may not expose it, but when present it
    # must match the database owner before any archive bytes cross the boundary.
    actual_scope = getattr(sandbox, "user_scope", "")
    if actual_scope:
        from project.workspace import user_directory

        expected_scope = PurePosixPath(user_directory(user_id)).name
        if actual_scope != expected_scope:
            raise SkillRestoreScopeError(
                "sandbox user scope does not match skill owner"
            )

    owned = (
        [dict(item) for item in owned_skills]
        if owned_skills is not None
        else await list_owned_skills(user_id)
    )
    if installed_skills is None:
        live = await sandbox.list_skills()
        if not isinstance(live, list):
            raise RuntimeError("sandbox skill catalogue is invalid")
        installed = [dict(item) for item in live if isinstance(item, Mapping)]
    else:
        installed = [dict(item) for item in installed_skills]

    restored = False
    for owned_hint in owned:
        owned_dir = owned_hint.get("install_dir")
        owned_name = owned_hint.get("name")
        listed_match = next(
            (
                item
                for item in installed
                if (owned_dir and item.get("install_dir") == owned_dir)
                or (owned_name and item.get("name") == owned_name)
            ),
            None,
        )
        listed_live = listed_match is not None
        if not owned_hint.get("restore_available"):
            continue

        identifier = owned_hint.get("id") or owned_hint.get("library_id")
        if not isinstance(identifier, str) or not identifier:
            continue
        # A listed match is a non-destructive LKG and must never be overwritten.
        # When the aggregate view says the path is missing, verify that exact
        # directory once before upload so a stale negative cannot overwrite a
        # newer live package. This avoids downloading every live SKILL.md on
        # every model step while keeping the destructive direction fail-safe.
        if listed_live:
            continue
        get_live = getattr(sandbox, "get_skill", None)
        if callable(get_live) and (owned_dir or owned_name):
            verify_name = (
                (listed_match or {}).get("install_dir")
                or (listed_match or {}).get("name")
                or owned_dir
                or owned_name
            )
            try:
                await get_live(str(verify_name))
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status != 404 and not isinstance(exc, (FileNotFoundError, LookupError)):
                    log.debug(
                        "Could not verify personal skill id=%s error_type=%s",
                        identifier[:64],
                        type(exc).__name__,
                    )
                    continue
                listed_live = False
            else:
                listed_live = True
        if listed_live:
            continue
        try:
            conflict = False
            fenced = False
            snapshot_name = ""
            # Hold the owner row lock through the execution-plane claim. If a
            # restore wins first, uninstall waits then removes it; if uninstall
            # wins first, this query sees a tombstone. The Action Server fence
            # supplies the same ordering when SQLite cannot lock the row and
            # when a backend dies between the two systems.
            async with get_db_session() as session:
                row = await _owned_row(
                    session,
                    user_id,
                    identifier,
                    for_update=True,
                )
                if row is None:
                    continue
                archive = row.archive_data
                install_dir = _required_text(row.install_dir, "install_dir")
                snapshot_name = row.name
                if not isinstance(archive, bytes) or not archive:
                    raise ValueError("personal skill archive is unavailable")
                if sha256(archive).hexdigest() != row.archive_sha256:
                    raise ValueError("personal skill archive checksum mismatch")
                try:
                    result = await sandbox.upload_skill_archive(
                        archive,
                        f"{install_dir}.zip",
                        install_dir,
                        create_only=True,
                        restore_generation=row.lifecycle_generation,
                    )
                except SkillArchiveAlreadyExistsError:
                    conflict = True
                    result = None
                except SkillRestoreFencedError:
                    fenced = True
                    result = None

            if fenced:
                # A newer durable delete owns this slug. It is intentionally
                # absent from both the returned catalogue and retry logs.
                continue
            if conflict:
                # Another backend/process won the create-if-absent race. That
                # is a successful recovery outcome, not a reason to retry with
                # destructive update semantics. Re-read the live package so
                # this catalogue can converge without waiting for another step.
                conflict_live = None
                if callable(get_live):
                    try:
                        conflict_live = await get_live(install_dir)
                    except Exception as exc:
                        log.debug(
                            "Could not read concurrently restored personal skill "
                            "id=%s error_type=%s",
                            identifier[:64],
                            type(exc).__name__,
                        )
                if isinstance(conflict_live, Mapping):
                    installed.append(
                        {
                            **dict(conflict_live),
                            "source": conflict_live.get("source") or "container",
                        }
                    )
                restored = True
                continue
            installed.append({
                **owned_hint,
                **(result if isinstance(result, dict) else {}),
                "name": snapshot_name or owned_name or install_dir,
                "install_dir": install_dir,
                "source": "container",
            })
            restored = True
        except Exception as exc:
            # One bad snapshot must not hide unrelated live Skills or stop an
            # Agent step. The next catalogue build gets another recovery chance.
            log.warning(
                "Could not restore personal skill id=%s error_type=%s",
                identifier[:64],
                type(exc).__name__,
            )

    if restored:
        try:
            refreshed = await sandbox.list_skills()
            if isinstance(refreshed, list):
                return [dict(item) for item in refreshed if isinstance(item, Mapping)]
        except Exception as exc:
            log.debug(
                "Could not refresh sandbox skills after restore error_type=%s",
                type(exc).__name__,
            )
    return installed


async def publish_personal_skill(user_id: str, identifier: str) -> dict[str, Any]:
    """Atomically copy the owner's current draft into the public release."""
    user_id = _required_text(user_id, "user_id")
    identifier = _required_text(identifier, "identifier")
    async with get_db_session() as session:
        row = await _owned_row(session, user_id, identifier, for_update=True)
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
                    UserSkill.lifecycle_state == LIFECYCLE_ACTIVE,
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
                    UserSkill.lifecycle_state == LIFECYCLE_ACTIVE,
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
) -> list[dict[str, Any]]:
    """Attach durable product provenance to a sandbox/host skill listing."""
    user_id = _required_text(user_id, "user_id")
    async with get_db_session() as session:
        personal = list(
            (
                await session.execute(
                    select(UserSkill).where(
                        UserSkill.owner_id == user_id,
                        UserSkill.lifecycle_state == LIFECYCLE_ACTIVE,
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
                    UserSkill.lifecycle_state == LIFECYCLE_ACTIVE,
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


def _finalize_owned_skill_tombstone(row: UserSkill, now: datetime) -> None:
    """Erase package material while retaining slug generation monotonicity."""
    row.lifecycle_state = LIFECYCLE_DELETED
    row.status = UNPUBLISHED
    row.description = ""
    row.icon = ""
    row.archive_data = b""
    row.archive_sha256 = sha256(b"").hexdigest()
    row.archive_size = 0
    row.metadata_data = {}
    row.published_name = None
    row.published_install_dir = None
    row.published_description = None
    row.published_icon = None
    row.published_version = None
    row.published_archive_data = None
    row.published_archive_sha256 = None
    row.published_archive_size = None
    row.published_metadata_data = None
    row.published_at = None
    row.version += 1
    row.updated_at = now


async def uninstall_owned_skill(user_id: str, identifier: str, sandbox) -> dict:
    """Fence, remove, and tombstone one personal Skill as one ordered unit.

    The database row lock is held while the Action Server advances its durable
    restore fence and removes the package. A transaction failure leaves the
    prior active row intact; a response-lost server-side delete still leaves a
    higher execution fence that refuses the stale active generation.
    """
    user_id = _required_text(user_id, "user_id")
    identifier = _required_text(identifier, "identifier")
    if sandbox is None or not callable(getattr(sandbox, "uninstall_skill", None)):
        raise RuntimeError("sandbox Skill uninstall is unavailable")

    async with get_db_session() as session:
        row = await _owned_row(
            session,
            user_id,
            identifier,
            for_update=True,
            include_inactive=True,
        )
        if row is None:
            raise LookupError("Personal skill not found")
        if row.lifecycle_state == LIFECYCLE_DELETED:
            return {
                "ok": True,
                "already_absent": True,
                "message": f"Skill '{row.install_dir}' was already deleted",
            }
        if row.lifecycle_state == LIFECYCLE_ACTIVE:
            row.lifecycle_state = LIFECYCLE_DELETING
            row.lifecycle_generation += 1
            row.updated_at = _now()
            await session.flush()

        generation = row.lifecycle_generation
        result = await sandbox.uninstall_skill(
            row.install_dir,
            mutation_generation=generation,
        )
        await session.execute(
            delete(SkillInstall).where(SkillInstall.user_skill_id == row.id)
        )
        _finalize_owned_skill_tombstone(row, _now())
        await session.flush()
        return result if isinstance(result, dict) else {"ok": True}


async def delete_owned_skill(user_id: str, identifier: str) -> bool:
    """Logically delete one owned snapshot while retaining its generation.

    This database-only helper is used after no execution-plane operation is
    needed. HTTP uninstall uses :func:`uninstall_owned_skill` so the filesystem
    fence and tombstone are ordered together.
    """
    user_id = _required_text(user_id, "user_id")
    identifier = _required_text(identifier, "identifier")
    async with get_db_session() as session:
        row = await _owned_row(
            session,
            user_id,
            identifier,
            for_update=True,
            include_inactive=True,
        )
        if row is None or row.lifecycle_state == LIFECYCLE_DELETED:
            return False
        if row.lifecycle_state == LIFECYCLE_ACTIVE:
            row.lifecycle_generation += 1
        await session.execute(
            delete(SkillInstall).where(SkillInstall.user_skill_id == row.id)
        )
        _finalize_owned_skill_tombstone(row, _now())
        await session.flush()
        return True
