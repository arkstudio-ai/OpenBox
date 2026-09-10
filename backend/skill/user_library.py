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

from sqlalchemy import ColumnElement, and_, delete, func, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.sql import Subquery

from core.identifier import ascending
from db.base import get_db_session
from db.models.catalog_override import CatalogOverride
from db.models.skill_install import SkillInstall
from db.models.user import User
from db.models.user_skill import UserSkill
from skill.catalog import catalog_entry_origin, catalog_index


COMMUNITY_PREFIX = "community:"
#: What the author has decided.  ``withdrawn`` keeps the published_* snapshot
#: so the same release can be re-submitted and so moderation stays readable.
UNPUBLISHED = "unpublished"
PUBLISHED = "published"
WITHDRAWN = "withdrawn"

#: What the operator has decided.  Orthogonal to ``status`` on purpose: a
#: single merged column would let "publish a new version" overwrite a delisting.
LISTING_PENDING = "pending"
LISTING_LISTED = "listed"
LISTING_REJECTED = "rejected"
LISTING_DELISTED = "delisted"
LISTINGS = frozenset(
    {LISTING_PENDING, LISTING_LISTED, LISTING_REJECTED, LISTING_DELISTED}
)
#: Catalogue entries live in code and are never submitted, so they only ever
#: sit on or off the shelf.
CATALOG_LISTINGS = frozenset({LISTING_LISTED, LISTING_DELISTED})
CATALOG_KINDS = frozenset({"skill", "mcp"})


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


def _parse_catalog_id(catalog_id: str) -> tuple[str, str]:
    """Split a store key into ``(source, target)``.

    ``community:<user_skills.id>`` addresses a submission row; ``skill:<id>`` /
    ``mcp:<id>`` address an entry that lives in :mod:`skill.catalog`.  Every
    operator action takes one of these, so one parser decides which table it
    is about.
    """
    catalog_id = _required_text(catalog_id, "catalog_id", limit=96)
    prefix, separator, target = catalog_id.partition(":")
    if not separator or not target:
        raise ValueError("catalog_id must look like '<kind>:<id>'")
    if prefix == "community":
        return "community", target
    if prefix in CATALOG_KINDS:
        return prefix, target
    raise ValueError(f"Unknown catalog_id prefix: {prefix!r}")


def store_visible() -> ColumnElement[bool]:
    """The single definition of "the store shows and installs this row".

    Both halves are load bearing and belong to different people: ``status`` is
    the author's decision, ``listing`` the operator's.  Everything that browses,
    resolves or installs a community package filters through this so a delisted
    or still-unreviewed package cannot leak out of one forgotten query.  The
    archive check is an integrity guard, not a third policy: a row claiming to
    be published with no bytes behind it can only fail at install time.
    """
    from skill.catalog_admin import not_deleted
    return and_(
        not_deleted(_community_key()),
        UserSkill.status == PUBLISHED,
        UserSkill.listing == LISTING_LISTED,
        UserSkill.published_archive_data.is_not(None),
    )


def _community_key() -> ColumnElement[str]:
    """A community row's ``catalog_id`` expressed in SQL.

    Lets install counts join in the same query instead of one count per row.
    """
    return literal(COMMUNITY_PREFIX).concat(UserSkill.id)


def _install_counts() -> Subquery:
    """Installs per catalogue key, grouped once for a whole page of entries."""
    return (
        select(
            SkillInstall.catalog_id.label("catalog_id"),
            func.count(SkillInstall.id).label("installs"),
        )
        .group_by(SkillInstall.catalog_id)
        .subquery()
    )


def _has_published_snapshot(row: UserSkill) -> bool:
    """Return whether a complete public release is available for consumers."""
    return row.status == PUBLISHED and row.published_archive_data is not None


def _has_release_snapshot(row: UserSkill) -> bool:
    """Whether a release exists at all, whatever the author has since done.

    A withdrawn package still has one; that is what makes withdrawal reversible
    and its moderation history readable.
    """
    return row.published_archive_data is not None


def _publication_status(row: UserSkill) -> str:
    """The author-facing state of a row: published, withdrawn, or neither."""
    if _has_published_snapshot(row):
        return PUBLISHED
    if row.status == WITHDRAWN and _has_release_snapshot(row):
        return WITHDRAWN
    return UNPUBLISHED


def _has_unpublished_changes(row: UserSkill) -> bool:
    """Compare the current draft with the immutable public release."""
    if not _has_published_snapshot(row):
        return True
    return _release_differs_from_draft(row)


def _release_differs_from_draft(row: UserSkill) -> bool:
    """Whether publishing now would produce different bytes or metadata.

    Deliberately blind to ``status``: re-submitting a withdrawn package that
    nobody edited must not burn a version number for an identical release.
    """
    if not _has_release_snapshot(row):
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
    released = _has_release_snapshot(row)
    publication_status = _publication_status(row)
    result: dict[str, Any] = {
        "id": row.id,
        "library_id": row.id,
        "catalog_id": _community_id(row.id) if released else None,
        "name": row.name,
        "title": row.name,
        "install_dir": row.install_dir,
        "description": row.description,
        "icon": row.icon,
        "category": "personal",
        "publication_status": publication_status,
        "status": publication_status,
        # Moderation state, so the author's own list can say *why* a package is
        # not on the shelf.  A draft that was never submitted has no listing to
        # report — the column's default would otherwise read as "on the shelf".
        "listing": row.listing if released else None,
        "listing_note": row.listing_note if released else None,
        "is_official": bool(row.is_official),
        "featured": bool(row.featured),
        "version": row.version,
        "draft_version": row.version,
        "published_version": row.published_version if released else None,
        "archive_size": row.archive_size,
        "published_archive_size": row.published_archive_size if released else None,
        "has_unpublished_changes": _has_unpublished_changes(row),
        "restore_available": bool(row.archive_data) and not metadata.get("admin_restore_disabled", False),
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
        # The author and their space, so a moderation action can reach the
        # person it affects instead of re-querying for them.
        "owner_id": row.owner_id,
        "workspace_id": row.workspace_id,
        "name": row.published_name,
        "title": row.published_name,
        "install_dir": row.published_install_dir,
        "description": row.published_description or "",
        "icon": row.published_icon or "",
        "listing": row.listing,
        "listing_note": row.listing_note,
        "is_official": bool(row.is_official),
        "featured": bool(row.featured),
        "origin": "official" if row.is_official else "community",
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
    had_withdrawn_release = row.status == WITHDRAWN and _has_release_snapshot(row)
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
    # release that users may already be installing, and editing after a
    # withdrawal is not a re-submission — only explicit publish switches the
    # public snapshot or puts the package back in front of anybody.
    if had_published_snapshot:
        row.status = PUBLISHED
    elif had_withdrawn_release:
        row.status = WITHDRAWN
    else:
        row.status = UNPUBLISHED
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
                    # Spelled out rather than left to the column's server
                    # default: an unfetched server default is invisible to the
                    # dict this returns.  The value is inert until publish,
                    # which decides the real listing.
                    listing=LISTING_LISTED,
                    is_official=False,
                    featured=False,
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


def _listing_after_publish(
    row: UserSkill, *, review_required: bool, publisher_is_admin: bool
) -> str:
    """Decide where a freshly submitted release lands on the shelf.

    Three rules, in this order:

    * An admin publishing is the store's own editorial act, so it lists
      immediately whether or not review is on for everyone else.
    * With review on, every submission queues — first release, and equally a
      re-submission after a rejection or a delisting, because the thing a
      reviewer rejected is exactly the thing that must not slip back on the
      shelf unseen.
    * With review off, publishing lists immediately *unless* an operator has
      delisted this package.  Pushing a new version is not an appeal: if
      updating could relist, delisting would be a suggestion rather than a
      decision.
    """
    if publisher_is_admin:
        return LISTING_LISTED
    if review_required:
        return LISTING_PENDING
    if row.listing == LISTING_DELISTED:
        return LISTING_DELISTED
    return LISTING_LISTED


async def publish_personal_skill(
    user_id: str,
    identifier: str,
    workspace_id: str | None = None,
    *,
    review_required: bool = False,
    publisher_is_admin: bool = False,
) -> dict[str, Any]:
    """Atomically copy the owner's current draft into the public release.

    ``review_required`` and ``publisher_is_admin`` are decided by the caller —
    the HTTP layer reads ``skill_store_review`` and the publisher's role — and
    together they choose the listing.  They default to the pre-review
    behaviour: a caller that has not been taught about review must not silently
    park every submission in a queue nobody is watching.
    """
    user_id = _required_text(user_id, "user_id")
    identifier = _required_text(identifier, "identifier")
    async with get_db_session() as session:
        row = await _owned_row(
            session, user_id, identifier,
            workspace_id=workspace_id, for_update=True,
        )
        if row is None:
            raise LookupError("Personal skill not found")
        now = _now()
        if _release_differs_from_draft(row):
            row.published_name = row.name
            row.published_install_dir = row.install_dir
            row.published_description = row.description
            row.published_icon = row.icon
            row.published_version = (row.published_version or 0) + 1
            row.published_archive_data = row.archive_data
            row.published_archive_sha256 = row.archive_sha256
            row.published_archive_size = row.archive_size
            row.published_metadata_data = dict(row.metadata_data or {})
            # published_at dates the release, not the click: a re-submission of
            # unchanged bytes must not reshuffle the store's ordering.
            row.published_at = now

        listing = _listing_after_publish(
            row,
            review_required=review_required,
            publisher_is_admin=publisher_is_admin,
        )
        if listing != LISTING_DELISTED:
            # A stale rejection reason next to a "pending" chip reads as a
            # fresh verdict.  The delist case keeps its note because the row
            # stays delisted and the author is owed the reason.
            row.listing_note = None
        row.listing = listing
        row.listing_changed_by = user_id
        # When the queue orders by submission time this is the honest key;
        # published_at belongs to the release.
        row.listing_changed_at = now
        if publisher_is_admin:
            row.is_official = True
        row.status = PUBLISHED
        row.updated_at = now
        await session.flush()
        return _snapshot_dict(row)


async def withdraw_personal_skill(
    user_id: str, identifier: str, workspace_id: str | None = None
) -> dict[str, Any]:
    """Take an author's own release out of the store, reversibly.

    Only ``status`` moves.  The ``published_*`` snapshot and the operator's
    ``listing`` both survive, so the author can re-submit the same release and
    a moderator can still see what was reviewed and why.
    """
    user_id = _required_text(user_id, "user_id")
    identifier = _required_text(identifier, "identifier")
    async with get_db_session() as session:
        row = await _owned_row(
            session, user_id, identifier,
            workspace_id=workspace_id, for_update=True,
        )
        if row is None:
            raise LookupError("Personal skill not found")
        if not _has_release_snapshot(row):
            raise ValueError("This skill has never been published")
        if row.status != WITHDRAWN:
            row.status = WITHDRAWN
            row.updated_at = _now()
            await session.flush()
        return _snapshot_dict(row)


async def get_published_skill(
    identifier: str,
    *,
    include_archive: bool = False,
    require_listed: bool = True,
) -> dict[str, Any] | None:
    """Resolve a public snapshot by its opaque ``community:<id>`` identifier.

    ``require_listed=False`` is for the review console alone: a moderator has
    to read and download exactly the submission nobody else may install yet.
    Every install path leaves it at its default.

    A disabled or deleted author takes their work off the shelf with them, so
    that check belongs to store visibility — not to the moderator's view. The
    console's queue (:func:`list_all_store_entries`) does not filter on owner
    status either, and a queue row whose detail page 404s is exactly the
    submission a reviewer most needs to read: the one whose account somebody
    already disabled.
    """
    identifier = _required_text(identifier, "identifier")
    row_id = _row_id(identifier)
    visibility = [
        store_visible(),
        User.is_active.is_(True),
        User.is_deleted.is_(False),
    ] if require_listed else [
        UserSkill.status.in_((PUBLISHED, WITHDRAWN)),
        UserSkill.published_archive_data.is_not(None),
    ]
    async with get_db_session() as session:
        row = (
            await session.execute(
                select(UserSkill)
                .join(User, User.id == UserSkill.owner_id)
                .where(UserSkill.id == row_id, *visibility)
            )
        ).scalar_one_or_none()
        result = (
            _published_snapshot_dict(row, include_archive=include_archive)
            if row
            else None
        )
    if result:
        from skill.catalog_admin import apply_metadata
        items = await apply_metadata([result], include_archive=include_archive)
        return items[0] if items else None
    return None


#: Everything the store renders about a community entry.  Naming the columns
#: keeps both archive blobs out of a list query — a page of entries would
#: otherwise drag every published ZIP through the connection to show a title.
_CATALOG_COLUMNS = (
    UserSkill.published_name,
    UserSkill.published_icon,
    UserSkill.published_description,
    UserSkill.published_metadata_data,
    UserSkill.published_version,
    UserSkill.published_at,
    UserSkill.is_official,
    UserSkill.featured,
    UserSkill.listing,
)


async def list_published_catalog_entries() -> list[dict[str, Any]]:
    """Return public, JSON-safe catalogue entries with no archive material.

    Only what :func:`store_visible` admits, ordered the way the store reads:
    featured first, then what people actually install, then newest.
    """
    counts = _install_counts()
    installs = func.coalesce(counts.c.installs, 0).label("installs")
    async with get_db_session() as session:
        rows = (
            await session.execute(
                select(UserSkill, User.username, installs)
                .join(User, User.id == UserSkill.owner_id)
                .outerjoin(counts, counts.c.catalog_id == _community_key())
                .options(load_only(*_CATALOG_COLUMNS))
                .where(
                    store_visible(),
                    User.is_active.is_(True),
                    User.is_deleted.is_(False),
                )
                .order_by(
                    UserSkill.featured.desc(),
                    installs.desc(),
                    UserSkill.published_at.desc(),
                    UserSkill.published_name.asc(),
                )
            )
        ).all()

    entries: list[dict[str, Any]] = []
    for row, publisher, installs_count in rows:
        metadata = row.published_metadata_data or {}
        requirements = list(metadata.get("requires_mcp") or [])
        tags = list(metadata.get("tags") or [])
        if "community" not in tags:
            tags.append("community")
        entries.append(
            {
                "id": _community_id(row.id),
                "catalog_id": _community_id(row.id),
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
                # An admin's own submission sits on the official shelf; every
                # other author's sits on the community one.
                "origin": "official" if row.is_official else "community",
                "official": bool(row.is_official),
                "featured": bool(row.featured),
                "listing": row.listing,
                "installs_count": int(installs_count or 0),
                "version": row.published_version,
                "published_at": _iso(row.published_at),
            }
        )
    from skill.catalog_admin import apply_metadata
    return await apply_metadata(entries)


#: The operator's view adds identity, moderation and integrity columns.  Both
#: archive blobs stay out for the same reason as the public projection.
_STORE_COLUMNS = _CATALOG_COLUMNS + (
    UserSkill.owner_id,
    UserSkill.workspace_id,
    UserSkill.name,
    UserSkill.install_dir,
    UserSkill.status,
    UserSkill.listing_note,
    UserSkill.listing_changed_by,
    UserSkill.listing_changed_at,
    UserSkill.published_install_dir,
    UserSkill.published_archive_sha256,
    UserSkill.published_archive_size,
    UserSkill.created_at,
    UserSkill.updated_at,
)

_STORE_SORTS = {"recent", "oldest", "installs", "name"}


async def list_all_store_entries(
    *,
    listing: str | None = None,
    status: str | None = None,
    official: bool | None = None,
    query: str | None = None,
    sort: str = "recent",
    offset: int = 0,
    limit: int = 50,
    with_total: bool = True,
) -> dict[str, Any]:
    """Every community submission, in every listing state, for the console.

    Unlike :func:`list_published_catalog_entries` this deliberately ignores
    :func:`store_visible` — a moderator's whole job is the rows the store is
    hiding.  Private drafts are still excluded: a package nobody ever submitted
    is not a store entry, and the console has no business reading it.

    ``with_total=False`` returns ``total=None`` and skips the count query.  It
    is for the store view, which assembles one page out of consecutive windows
    under unchanging filters: the count is the same answer every time, and
    asking for it once is the difference between one extra query per page and
    one per window.
    """
    if listing is not None and listing not in LISTINGS:
        raise ValueError(f"Unknown listing: {listing!r}")
    if status is not None and status not in {PUBLISHED, WITHDRAWN}:
        raise ValueError(f"Unknown status: {status!r}")
    if sort not in _STORE_SORTS:
        raise ValueError(f"Unknown sort: {sort!r}")
    # A page cap the caller cannot raise: these rows are wide and the console
    # is the one place that reads all of them.
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    from skill.catalog_admin import apply_metadata, not_deleted, overlays
    filters = [
        not_deleted(_community_key()),
        # "Has been submitted at least once", asked without naming the blob.
        UserSkill.published_version.is_not(None),
    ]
    if listing is not None:
        filters.append(UserSkill.listing == listing)
    if status is not None:
        filters.append(UserSkill.status == status)
    if official is not None:
        filters.append(UserSkill.is_official.is_(bool(official)))
    if query and query.strip():
        # One box over author and package, because an operator chasing a report
        # has either a username, an email or a skill name and rarely knows
        # which field it belongs to.
        pattern = f"%{query.strip()}%"
        managed_matches = [key.split(":", 1)[1] for key, entry in (await overlays()).items()
                           if key.startswith(COMMUNITY_PREFIX) and query.strip().lower() in
                           " ".join(str(entry.get(k) or "") for k in ("title", "description")).lower()]
        filters.append(
            or_(
                UserSkill.id.in_(managed_matches),
                UserSkill.published_name.ilike(pattern),
                UserSkill.name.ilike(pattern),
                User.username.ilike(pattern),
                User.email.ilike(pattern),
            )
        )

    counts = _install_counts()
    installs = func.coalesce(counts.c.installs, 0).label("installs")
    order = {
        "recent": (UserSkill.published_at.desc(), UserSkill.id.desc()),
        # Oldest first is the review queue's order: the person who has been
        # waiting longest is seen first.
        "oldest": (UserSkill.published_at.asc(), UserSkill.id.asc()),
        "installs": (installs.desc(), UserSkill.published_at.desc()),
        "name": (UserSkill.published_name.asc(), UserSkill.id.asc()),
    }[sort]

    async with get_db_session() as session:
        total = (
            (
                await session.execute(
                    select(func.count())
                    .select_from(UserSkill)
                    .join(User, User.id == UserSkill.owner_id)
                    .where(*filters)
                )
            ).scalar_one()
            if with_total
            else None
        )
        rows = (
            await session.execute(
                select(UserSkill, User.username, User.email, installs)
                .join(User, User.id == UserSkill.owner_id)
                .outerjoin(counts, counts.c.catalog_id == _community_key())
                .options(load_only(*_STORE_COLUMNS))
                .where(*filters)
                .order_by(*order)
                .offset(offset)
                .limit(limit)
            )
        ).all()

    entries: list[dict[str, Any]] = []
    for row, username, email, installs_count in rows:
        metadata = row.published_metadata_data or {}
        entries.append(
            {
                "catalog_id": _community_id(row.id),
                "id": _community_id(row.id),
                "library_id": row.id,
                "kind": "skill",
                "origin": "official" if row.is_official else "community",
                "name": row.published_name or row.name,
                "title": row.published_name or row.name,
                "icon": row.published_icon or "",
                "description": row.published_description or "",
                "install_dir": row.published_install_dir or row.install_dir,
                "author": {
                    "user_id": row.owner_id,
                    "username": username,
                    "email": email,
                },
                "workspace_id": row.workspace_id,
                "version": row.published_version,
                "published_at": _iso(row.published_at),
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
                "installs_count": int(installs_count or 0),
                "status": row.status,
                "listing": row.listing,
                "listing_note": row.listing_note,
                "listing_changed_by": row.listing_changed_by,
                "listing_changed_at": _iso(row.listing_changed_at),
                "featured": bool(row.featured),
                "is_official": bool(row.is_official),
                "requires_mcp": list(metadata.get("requires_mcp") or []),
                "homepage": metadata.get("homepage", ""),
                "size": row.published_archive_size,
                "sha256": row.published_archive_sha256,
            }
        )
    return {
        "total": int(total) if total is not None else None,
        "entries": await apply_metadata(entries),
    }


def _moderation_result(
    *,
    catalog_id: str,
    source: str,
    field: str,
    previous: Any,
    current: Any,
    changed: bool,
    listing: str,
    featured: bool,
    is_official: bool,
    note: str | None = None,
    owner_id: str | None = None,
    workspace_id: str | None = None,
    name: str | None = None,
    version: int | None = None,
) -> dict[str, Any]:
    """What an operator action reports back for auditing and notification.

    ``changed`` is the honest answer to "did this call move anything": a repeat
    of a decision already taken must not write a second audit entry or send the
    author a second notice for something that did not happen again.
    """
    return {
        "catalog_id": catalog_id,
        "source": source,
        "field": field,
        "previous": previous,
        "current": current,
        "changed": changed,
        "listing": listing,
        "featured": featured,
        "is_official": is_official,
        "note": note,
        # Present only for a community submission — a catalogue entry has no
        # author to tell.
        "owner_id": owner_id,
        "workspace_id": workspace_id,
        "name": name,
        "version": version,
    }


def _catalog_defaults(catalog_id: str) -> tuple[str, bool, bool]:
    """The shelf state an entry has with no override row: what the code says.

    An unknown id (an operator overlay entry we do not ship) falls back to
    "listed, not featured" rather than 404ing, so a decision about it is still
    recordable.
    """
    entry = catalog_index().get(catalog_id) or {}
    listing = entry.get("listing")
    if listing not in CATALOG_LISTINGS:
        listing = LISTING_LISTED
    return (
        listing,
        bool(entry.get("featured", False)),
        catalog_entry_origin(entry) == "official",
    )


async def _mutate_catalog_override(
    catalog_id: str,
    *,
    field: str,
    listing: str | None = None,
    featured: bool | None = None,
    note: str | None = None,
    actor_user_id: str | None = None,
) -> dict[str, Any]:
    """Upsert one operator decision about a code-defined catalogue entry."""
    async with get_db_session() as session:
        row = (
            await session.execute(
                select(CatalogOverride)
                .where(CatalogOverride.catalog_id == catalog_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        default_listing, default_featured, is_official = _catalog_defaults(catalog_id)
        current_listing = row.listing if row is not None else default_listing
        current_featured = row.featured if row is not None else default_featured
        current_note = row.note if row is not None else None

        new_listing = current_listing if listing is None else listing
        new_featured = current_featured if featured is None else bool(featured)
        new_note = current_note if listing is None else note
        changed = (
            new_listing != current_listing
            or new_featured != current_featured
            or new_note != current_note
        )
        if changed:
            if row is None:
                row = CatalogOverride(
                    catalog_id=catalog_id,
                    listing=new_listing,
                    featured=new_featured,
                    note=new_note,
                    changed_by=actor_user_id,
                    changed_at=_now(),
                )
                session.add(row)
            else:
                row.listing = new_listing
                row.featured = new_featured
                row.note = new_note
                row.changed_by = actor_user_id
                row.changed_at = _now()
            await session.flush()

    previous = current_listing if field == "listing" else current_featured
    current = new_listing if field == "listing" else new_featured
    return _moderation_result(
        catalog_id=catalog_id,
        source="catalog",
        field=field,
        previous=previous,
        current=current,
        changed=changed,
        listing=new_listing,
        featured=new_featured,
        # Catalogue entries carry no author, so "official" follows the
        # publisher named in code rather than being an operator's toggle.
        is_official=is_official,
        note=new_note,
    )


async def set_listing(
    catalog_id: str,
    listing: str,
    *,
    note: str | None = None,
    actor_user_id: str | None = None,
) -> dict[str, Any] | None:
    """Put a store entry on or off the shelf, or resolve a submission.

    Returns ``None`` when the target does not exist, and a result whose
    ``changed`` is ``False`` when the decision was already in force — repeating
    an action is a no-op, not an error, because the console retries and two
    operators can click the same button.
    """
    source, target = _parse_catalog_id(catalog_id)
    note = note.strip() if isinstance(note, str) and note.strip() else None

    if source != "community":
        if listing not in CATALOG_LISTINGS:
            raise ValueError(
                f"A catalogue entry can only be {sorted(CATALOG_LISTINGS)}"
            )
        return await _mutate_catalog_override(
            catalog_id,
            field="listing",
            listing=listing,
            note=note,
            actor_user_id=actor_user_id,
        )

    if listing not in LISTINGS:
        raise ValueError(f"Unknown listing: {listing!r}")
    async with get_db_session() as session:
        row = (
            await session.execute(
                select(UserSkill).where(UserSkill.id == target).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        previous = row.listing
        # An unaccompanied decision clears the old reason: approving with last
        # week's rejection note still attached would show the author a verdict
        # that no longer describes anything.
        changed = previous != listing or (row.listing_note or None) != note
        if changed:
            row.listing = listing
            row.listing_note = note
            row.listing_changed_by = actor_user_id
            row.listing_changed_at = _now()
            await session.flush()
        return _moderation_result(
            catalog_id=_community_id(row.id),
            source="community",
            field="listing",
            previous=previous,
            current=listing,
            changed=changed,
            listing=row.listing,
            featured=bool(row.featured),
            is_official=bool(row.is_official),
            note=row.listing_note,
            owner_id=row.owner_id,
            workspace_id=row.workspace_id,
            name=row.published_name or row.name,
            version=row.published_version,
        )


async def set_featured(
    catalog_id: str,
    featured: bool,
    *,
    actor_user_id: str | None = None,
) -> dict[str, Any] | None:
    """Pin a store entry to the top of its section, or unpin it."""
    source, target = _parse_catalog_id(catalog_id)
    featured = bool(featured)
    if source != "community":
        return await _mutate_catalog_override(
            catalog_id,
            field="featured",
            featured=featured,
            actor_user_id=actor_user_id,
        )

    async with get_db_session() as session:
        row = (
            await session.execute(
                select(UserSkill).where(UserSkill.id == target).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        previous = bool(row.featured)
        changed = previous != featured
        if changed:
            # listing_changed_* stays put: it dates the shelf decision the
            # author is shown, and the audit log already records who pinned
            # what and when.
            row.featured = featured
            await session.flush()
        return _moderation_result(
            catalog_id=_community_id(row.id),
            source="community",
            field="featured",
            previous=previous,
            current=featured,
            changed=changed,
            listing=row.listing,
            featured=bool(row.featured),
            is_official=bool(row.is_official),
            note=row.listing_note,
            owner_id=row.owner_id,
            workspace_id=row.workspace_id,
            name=row.published_name or row.name,
            version=row.published_version,
        )


async def set_official(
    catalog_id: str,
    is_official: bool,
    *,
    actor_user_id: str | None = None,
) -> dict[str, Any] | None:
    """Move a submission between the official and community shelves.

    Only submissions: a catalogue entry's shelf follows the publisher named in
    code, so promoting one would mean claiming somebody else's work as ours.
    """
    source, target = _parse_catalog_id(catalog_id)
    if source != "community":
        raise ValueError(
            "Only a community submission can be marked official; a catalogue "
            "entry's origin follows its publisher"
        )
    is_official = bool(is_official)

    async with get_db_session() as session:
        row = (
            await session.execute(
                select(UserSkill).where(UserSkill.id == target).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        previous = bool(row.is_official)
        changed = previous != is_official
        if changed:
            row.is_official = is_official
            await session.flush()
        return _moderation_result(
            catalog_id=_community_id(row.id),
            source="community",
            field="official",
            previous=previous,
            current=is_official,
            changed=changed,
            listing=row.listing,
            featured=bool(row.featured),
            is_official=bool(row.is_official),
            note=row.listing_note,
            owner_id=row.owner_id,
            workspace_id=row.workspace_id,
            name=row.published_name or row.name,
            version=row.published_version,
        )


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
                    select(SkillInstall).where(
                        SkillInstall.user_id == user_id,
                        # This annotates a *skill* listing. MCP provenance rows
                        # share the ``install_dir`` namespace only by accident —
                        # a server's key there is its name — so an installed
                        # server called "memory" must not claim the skill of the
                        # same name as a store install.
                        SkillInstall.kind == "skill",
                    )
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
        # Moderation state travels with the author's own copy so their list can
        # say "rejected, because…" instead of only "not in the store".
        skill["listing"] = None
        skill["listing_note"] = None
        skill["is_official"] = False
        if category == "personal":
            released = bool(
                personal_row is not None and _has_release_snapshot(personal_row)
            )
            skill["publication_status"] = (
                _publication_status(personal_row)
                if personal_row is not None
                else UNPUBLISHED
            )
            skill["library_id"] = personal_row.id if personal_row is not None else None
            skill["catalog_id"] = (
                _community_id(personal_row.id)
                if released
                else None
            )
            skill["published_at"] = (
                _iso(personal_row.published_at) if personal_row is not None else None
            )
            if personal_row is not None:
                skill["listing"] = personal_row.listing if released else None
                skill["listing_note"] = personal_row.listing_note if released else None
                skill["is_official"] = bool(personal_row.is_official)
        elif category == "store":
            skill["publication_status"] = None
            skill["library_id"] = None
            # The recorded key, not one rebuilt from user_skill_id: a
            # catalogue install has no user_skills row behind it.
            skill["catalog_id"] = store_row.catalog_id
            skill["published_at"] = None
        else:
            skill["publication_status"] = None
            skill["library_id"] = None
            skill["catalog_id"] = None
            skill["published_at"] = None
        annotated.append(skill)

    return annotated


async def record_store_installation(
    *,
    user_id: str,
    catalog_id: str,
    name: str,
    install_dir: str,
    kind: str | None = None,
    user_skill_id: str | None = None,
) -> dict[str, Any]:
    """Upsert provenance after a store entry installs successfully.

    One record for both halves of the store.  A community package is checked
    against :func:`store_visible` first, because provenance must never claim
    somebody installed something the store was not offering; a catalogue entry
    lives in code and is identified by ``<kind>:<id>`` alone.
    """
    user_id = _required_text(user_id, "user_id")
    name = _required_text(name, "name")
    install_dir = _required_text(install_dir, "install_dir")
    source, target = _parse_catalog_id(catalog_id)
    if source == "community":
        # The prefix is the authority: a community row is always a skill, and
        # its user_skills id is spelled inside the key.
        kind = "skill"
        user_skill_id = _required_text(user_skill_id or target, "user_skill_id")
        catalog_id = _community_id(user_skill_id)
    else:
        # The key already names the kind; an explicit argument is only ever a
        # cross-check, never a second source of truth that could disagree.
        if kind is not None and kind != source:
            raise ValueError(
                f"catalog_id {catalog_id!r} disagrees with kind {kind!r}"
            )
        kind = source
        user_skill_id = None
    now = _now()

    async with get_db_session() as session:
        if user_skill_id is not None:
            published = (
                await session.execute(
                    select(UserSkill.id)
                    .join(User, User.id == UserSkill.owner_id)
                    .where(
                        UserSkill.id == user_skill_id,
                        store_visible(),
                        User.is_active.is_(True),
                        User.is_deleted.is_(False),
                    )
                    .with_for_update(of=UserSkill)
                )
            ).scalar_one_or_none()
            if published is None:
                raise LookupError("Published skill not found")

        # Keyed the same way ``uq_skill_installs_user_kind_dir`` is: a skill's
        # sandbox directory and an MCP server's name are separate namespaces
        # and do collide (the catalogue ships a server called "memory", and a
        # community skill may be called that too). Matching on the directory
        # alone would re-point the skill's provenance row at the server.
        row = (
            await session.execute(
                select(SkillInstall)
                .where(
                    SkillInstall.user_id == user_id,
                    SkillInstall.kind == kind,
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
                kind=kind,
                catalog_id=catalog_id,
                name=name,
                install_dir=install_dir,
                installed_at=now,
            )
            session.add(row)
        else:
            # A directory can only hold one package, so re-installing over it
            # re-points the record instead of leaving a lie behind.
            row.user_skill_id = user_skill_id
            row.kind = kind
            row.catalog_id = catalog_id
            row.name = name
            row.installed_at = now
        await session.flush()
        return {
            "id": row.id,
            "name": row.name,
            "install_dir": row.install_dir,
            "category": "store",
            "kind": row.kind,
            "publication_status": None,
            "library_id": None,
            "catalog_id": row.catalog_id,
            "installed_at": _iso(row.installed_at),
        }


async def remove_store_installation(
    user_id: str, identifier: str, *, kind: str | None = None
) -> bool:
    """Remove only this user's provenance row after its sandbox uninstall.

    Callers arrive from three directions — an install directory, a display
    name, or a store key — so all three match.  ``kind`` narrows that when a
    skill and an MCP server share a name, which they may: they are installed
    into different places and only the caller knows which one just went away.
    """
    user_id = _required_text(user_id, "user_id")
    identifier = _required_text(identifier, "identifier")
    row_id = _row_id(identifier)
    async with get_db_session() as session:
        result = await session.execute(
            delete(SkillInstall).where(
                SkillInstall.user_id == user_id,
                or_(
                    SkillInstall.install_dir == identifier,
                    SkillInstall.name == identifier,
                    SkillInstall.catalog_id == identifier,
                    SkillInstall.user_skill_id == row_id,
                ),
                *([SkillInstall.kind == kind] if kind else []),
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
