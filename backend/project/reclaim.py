"""Reclaiming project directories whose sessions are gone.

Directories used to accumulate forever: nothing removed them when a session was
deleted, and the WUYING provider's delete_container is a no-op, so the usual
"the container goes away eventually" safety net does not exist either.

Deleted material goes to .openbox/trash first and is removed from there on a
later pass. An agent can put hours of work in a project directory, and a
reclaim job that deletes outright turns one wrong retention setting into
permanent loss.

A desktop belongs to one workspace, and a slug is unique only per (workspace,
user), so a directory is compared with the projects of the workspace that owns
the desktop being scanned. Another tenant having deleted a project of the same
name says nothing about this directory. When the owning workspace is not known
nothing is moved and nothing is purged (docs/kernel-refactor/project-reaper.md).
"""
from __future__ import annotations

import re
import shlex
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select

from core.log import create_logger
from db.base import get_db_session
from db.models.project import Project as ProjectORM
from project.workspace import SLUG_RE, TRASH_ROOT, WORKSPACE_ROOT

log = create_logger("project.reclaim")

#: How long a soft-deleted project keeps its directory before it is binned.
DELETED_GRACE_DAYS = 7
#: How long binned material stays recoverable, counted from when it was binned.
TRASH_GRACE_DAYS = 14

#: Both writers of the trash (this module and workspace.delete_project) name an
#: entry <slug>-<UTC time of the move>. The name is the only record of when an
#: entry was binned: `mv` keeps a directory's own mtime, so `find -mtime` alone
#: measures how long ago the project's top level last changed, not its time in
#: the bin.
_BIN_STAMP = "%Y%m%d-%H%M%S"
_BIN_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}-(\d{8}-\d{6})")


async def _protected_slugs(workspace_id: str) -> set[str]:
    """Slugs that must not be touched on this workspace's desktop.

    Every live project in any workspace, as before: narrowing this set could
    only add moves. Plus this workspace's projects deleted too recently to
    reclaim, or with no deletion time to measure, so a later deletion of a slug
    is not cut short by an older deletion of the same slug.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=DELETED_GRACE_DAYS)
    async with get_db_session() as db:
        live = (await db.execute(
            select(ProjectORM.slug).where(ProjectORM.is_deleted == False)  # noqa: E712
        )).scalars().all()
        recent = (await db.execute(
            select(ProjectORM.slug).where(
                ProjectORM.workspace_id == workspace_id,
                ProjectORM.is_deleted == True,  # noqa: E712
                or_(ProjectORM.deleted_at == None, ProjectORM.deleted_at >= cutoff),  # noqa: E711
            )
        )).scalars().all()
    return {s for s in (*live, *recent) if s}


async def _expired_slugs(workspace_id: str) -> dict[str, datetime]:
    """This workspace's slugs deleted long enough ago, with the latest deletion."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=DELETED_GRACE_DAYS)
    async with get_db_session() as db:
        rows = (await db.execute(
            select(ProjectORM.slug, func.max(ProjectORM.deleted_at)).where(
                ProjectORM.workspace_id == workspace_id,
                ProjectORM.is_deleted == True,  # noqa: E712
                ProjectORM.deleted_at != None,  # noqa: E711
                ProjectORM.deleted_at < cutoff,
            ).group_by(ProjectORM.slug)
        )).all()
    return {slug: _utc(deleted_at) for slug, deleted_at in rows if slug and deleted_at}


def _utc(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; every deleted_at is written in UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _modified_at(sandbox, path: str) -> datetime | None:
    """The mtime of a directory's top level, or None when it cannot be read."""
    try:
        result = await sandbox.execute(
            f"stat -c %Y -- {shlex.quote(path)} 2>/dev/null || true", timeout=30)
        return datetime.fromtimestamp(int((result.stdout or "").strip()), timezone.utc)
    except Exception:
        return None


async def reclaim(sandbox, *, workspace_id: str | None = None, dry_run: bool = False) -> dict:
    """Bin directories for this workspace's deleted projects and empty the old trash.

    `workspace_id` is the workspace that owns the desktop `sandbox` talks to.
    Without it nothing is moved or purged: the directories cannot be
    attributed to anyone's projects.

    Only directories whose slug maps to a project of that workspace deleted
    more than DELETED_GRACE_DAYS ago, and whose top level has not changed since
    that deletion, are touched. Anything unrecognised is left alone and
    reported: a directory the database has never heard of is more likely to be
    something worth keeping than something worth deleting.
    """
    if sandbox is None:
        return {"binned": [], "purged": [], "unknown": [], "skipped": "no sandbox"}
    if not workspace_id:
        return {"binned": [], "purged": [], "unknown": [], "skipped": "no workspace"}

    protected = await _protected_slugs(workspace_id)
    expired = await _expired_slugs(workspace_id)

    try:
        result = await sandbox.execute(
            f"ls -1 {WORKSPACE_ROOT} 2>/dev/null || true", timeout=30)
        present = [d.strip() for d in (result.stdout or "").splitlines() if d.strip()]
    except Exception as e:
        log.warning(f"Could not list {WORKSPACE_ROOT}: {e}")
        return {"binned": [], "purged": [], "unknown": [], "error": str(e)}

    binned, unknown = [], []
    stamp = datetime.now(timezone.utc).strftime(_BIN_STAMP)
    for name in present:
        if name.startswith("."):
            continue  # .openbox and friends are ours
        if name in protected:
            continue
        deleted_at = expired.get(name)
        if deleted_at is None or not SLUG_RE.fullmatch(name):
            unknown.append(name)
            continue
        # A directory changed after its project was deleted is someone's
        # current work under an old name (an agent's mkdir or clone), not the
        # deleted project's leftovers.
        modified_at = await _modified_at(sandbox, f"{WORKSPACE_ROOT}/{name}")
        if modified_at is None or modified_at >= deleted_at:
            unknown.append(name)
            continue
        if not dry_run:
            await sandbox.execute(
                f"mkdir -p {TRASH_ROOT} && "
                f"mv {WORKSPACE_ROOT}/{name} {TRASH_ROOT}/{name}-{stamp} || true",
                timeout=60)
        binned.append(name)

    purged = await _purge_trash(sandbox, dry_run=dry_run)

    if binned or purged:
        log.info(f"Reclaim {workspace_id}: binned {len(binned)}, purged {len(purged)}")
    if unknown:
        log.info(f"Reclaim {workspace_id}: left {len(unknown)} unrecognised directories alone: {unknown}")
    return {"binned": binned, "purged": purged, "unknown": unknown}


def _binned_at(path: str) -> datetime | None:
    """When a trash entry was binned, read from its name; None if not ours."""
    name = path[len(TRASH_ROOT) + 1:]
    match = _BIN_NAME_RE.fullmatch(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), _BIN_STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def _purge_trash(sandbox, dry_run: bool = False) -> list[str]:
    """Delete trash binned longer ago than the recovery window."""
    try:
        result = await sandbox.execute(
            f"find {TRASH_ROOT} -maxdepth 1 -mindepth 1 -type d "
            f"-mtime +{TRASH_GRACE_DAYS} 2>/dev/null || true",
            timeout=30)
        stale = [d.strip() for d in (result.stdout or "").splitlines() if d.strip()]
    except Exception as e:
        log.warning(f"Could not scan trash: {e}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=TRASH_GRACE_DAYS)
    purged = []
    for path in stale:
        # Belt and braces: a bug that produced an empty path here would
        # otherwise expand to `rm -rf /`.
        if not path.startswith(TRASH_ROOT + "/"):
            log.warning(f"Refusing to purge path outside trash: {path}")
            continue
        # find's age is the directory's own mtime, which a move keeps: a
        # project untouched for a month would go the moment it was binned.
        # An entry whose name carries no bin time is not ours to judge.
        binned_at = _binned_at(path)
        if binned_at is None or binned_at >= cutoff:
            continue
        if not dry_run:
            await sandbox.execute(f"rm -rf {shlex.quote(path)}", timeout=120)
        purged.append(path)
    return purged
