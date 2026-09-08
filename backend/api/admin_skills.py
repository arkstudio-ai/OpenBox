"""Operator control of the skill store: the shelf, the review queue, installs.

Deliberately no ``get_workspace`` dependency (§4.2): the store is global, so an
``X-Workspace-Id`` header would scope nothing here while suggesting these reads
are tenant-bound. Audit entries carry the *subject's* workspace instead — the
author's space for a submission, nothing at all for a catalogue entry that
belongs to no tenant.

Every decision this module takes is made in :mod:`skill.user_library`; what
lives here is the HTTP shape of it, plus the two things an operator action owes
the rest of the system: an audit entry and a word to the author.
"""
from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from audit import record
from auth.middleware import require_admin
from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.notification import Notification
from db.models.skill_install import SkillInstall
from db.models.user import User
from db.models.user_skill import UserSkill
from skill import user_library
from skill.catalog import catalog_entry_origin, catalog_index, load_catalog


router = APIRouter(
    prefix="/api/admin/skills",
    tags=["admin-skills"],
    dependencies=[Depends(require_admin)],
)
log = create_logger("api.admin_skills")

ORIGINS = "official|community|third_party"
KINDS = "skill|mcp"
LISTINGS = "pending|listed|rejected|delisted"
SORTS = "recent|oldest|installs|name"
REVIEW_STATES = "pending|rejected"

#: A submission's ZIP is hostile input: it was uploaded by a stranger and is
#: being opened so somebody can decide whether to trust it. Nothing is ever
#: extracted to disk or executed; these caps bound what reading the manifest
#: can cost even when the archive was built to be expensive to read.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
#: Beyond this the package is not a skill, it is a stress test — say so rather
#: than turning a million central-directory records into a JSON response.
MAX_ARCHIVE_ENTRIES = 20_000
#: §4.9: list at most this many members, and admit the truncation in the body.
MAX_LISTED_FILES = 500
SKILL_MD_LIMIT = 64 * 1024
#: How many community rows one merge pass pulls. The store view interleaves two
#: sources, so the page the caller asked for has to be assembled from the front
#: of both; only the requested window is ever fetched.
MERGE_PAGE = 200
#: The deepest page /store will assemble. Merging two sources means reading
#: ``offset + limit`` rows before the requested slice exists, so an unbounded
#: offset makes the work grow with the table for a URL anyone can hand-edit.
#: Past this depth the answer is a filter, not another page — 25 windows is
#: already far more than an operator scrolls to.
MAX_STORE_OFFSET = MERGE_PAGE * 25

#: What the author is told when an operator moves their submission. A listing
#: that goes back to ``pending`` is the author's own re-submission, so there is
#: nothing to tell them they did not just do.
AUTHOR_NOTICES = {
    user_library.LISTING_LISTED: ("skill_approved", "技能已上架"),
    user_library.LISTING_REJECTED: ("skill_rejected", "技能未通过审核"),
    user_library.LISTING_DELISTED: ("skill_delisted", "技能已下架"),
}


class ListingBody(BaseModel):
    listing: str = Field(pattern=f"^({LISTINGS})$")
    note: str | None = None


class FeaturedBody(BaseModel):
    featured: bool


class OfficialBody(BaseModel):
    is_official: bool


class NoteBody(BaseModel):
    note: str


def _at(value: datetime | None) -> str | None:
    """SQLite hands back naive datetimes; clients only ever see UTC ISO."""
    if value is None:
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.isoformat()


def _is_community(catalog_id: str) -> bool:
    return catalog_id.startswith(user_library.COMMUNITY_PREFIX)


def _resource_type(catalog_id: str) -> str:
    return "user_skill" if _is_community(catalog_id) else "catalog_entry"


def _note(value: str | None, *, required: bool) -> str | None:
    """A reason is part of the decision, not a nicety (§4.9).

    Taking something off the shelf or rejecting it is the one thing the author
    cannot see the cause of, so the console may not do either silently.
    """
    text = value.strip() if isinstance(value, str) else ""
    if required and not text:
        raise HTTPException(422, detail="A reason is required for this decision")
    return text or None


async def _notify_author(result: dict) -> None:
    """Tell a submission's author what was just decided about it.

    Only a shelf decision is announced (§4.7). ``listing`` reports where the
    row now sits whatever moved it, so pinning or promoting an already-listed
    package would otherwise read to its author as a fresh approval.

    Best effort on purpose: the decision is already committed, so failing the
    request here would report a rollback that did not happen and the console's
    retry would be a no-op anyway. A catalogue entry has no author to tell.
    """
    if result.get("source") != "community" or not result.get("changed"):
        return
    if result.get("field") != "listing":
        return
    notice = AUTHOR_NOTICES.get(result.get("listing"))
    workspace_id = result.get("workspace_id")
    owner_id = result.get("owner_id")
    if notice is None or not workspace_id or not owner_id:
        return
    kind, title = notice
    name = result.get("name") or ""
    version = result.get("version")
    body = f"《{name}》{f'v{version} ' if version else ''}"
    if result.get("listing") == user_library.LISTING_LISTED:
        body += "已通过审核，现已在技能商店上架。"
    elif result.get("listing") == user_library.LISTING_REJECTED:
        body += f"未通过审核：{result.get('note') or '未填写原因'}。修改后可重新提交。"
    else:
        body += f"已被下架：{result.get('note') or '未填写原因'}。已安装的副本不受影响。"
    try:
        async with get_db_session() as session:
            session.add(Notification(
                id=ascending("ntf"),
                workspace_id=workspace_id,
                user_id=owner_id,
                kind=kind,
                title=title[:255],
                body=body,
                created_at=datetime.now(timezone.utc),
            ))
    except Exception as exc:
        log.warning(
            "Could not notify author=%s about %s: %s",
            owner_id, result.get("catalog_id"), exc,
        )


async def _settle(
    result: dict | None,
    *,
    action: str,
    admin: dict,
    request: Request,
    detail: dict,
) -> dict:
    """Record and announce one operator decision, exactly once.

    ``changed`` is the whole point: two operators clicking the same button, or
    one console retrying, must not produce two audit entries and two notices
    for a single decision (§4.9).
    """
    if result is None:
        raise HTTPException(404, detail="Store entry not found")
    if result["changed"]:
        await record(
            admin["user_id"], result.get("workspace_id"), action,
            _resource_type(result["catalog_id"]), result["catalog_id"], detail, request,
        )
        await _notify_author(result)
    return result


# ── Store shelf ────────────────────────────────────────────────────────────

async def _install_counts(catalog_ids: list[str]) -> dict[str, int]:
    """Installs per catalogue key, counted once for a whole page."""
    if not catalog_ids:
        return {}
    async with get_db_session() as session:
        rows = (await session.execute(
            select(SkillInstall.catalog_id, func.count(SkillInstall.id))
            .where(SkillInstall.catalog_id.in_(catalog_ids))
            .group_by(SkillInstall.catalog_id)
        )).all()
    return {catalog_id: int(count or 0) for catalog_id, count in rows}


def _catalog_row(entry: dict, counts: dict[str, int]) -> dict:
    """One code-defined catalogue entry, projected for the console.

    Built field by field rather than spread: the raw entry carries ``install``
    (a whole SKILL.md for the content skills) and ``config`` (an MCP server's
    command line and env), and a shelf listing has no use for either.
    """
    catalog_id = entry.get("catalog_id") or ""
    return {
        "catalog_id": catalog_id,
        "id": catalog_id,
        "source": "catalog",
        "kind": entry.get("kind"),
        "origin": entry.get("origin"),
        "name": entry.get("name") or "",
        "title": entry.get("title") or entry.get("name") or "",
        "icon": entry.get("icon") or "",
        "description": entry.get("description") or "",
        "publisher": entry.get("publisher") or "",
        "homepage": entry.get("homepage") or "",
        "author": None,
        "workspace_id": None,
        "library_id": None,
        "install_dir": None,
        "version": None,
        "published_at": None,
        "created_at": None,
        "updated_at": None,
        "installs_count": counts.get(catalog_id, 0),
        "status": None,
        "listing": entry.get("listing"),
        "listing_note": entry.get("listing_note"),
        "listing_changed_by": None,
        "listing_changed_at": None,
        "featured": bool(entry.get("featured")),
        "is_official": bool(entry.get("official")),
        "requires_mcp": list(entry.get("requires_mcp") or []),
        "size": None,
        "sha256": None,
    }


def _community_row(entry: dict) -> dict:
    """One submission, as the library already projected it (never any bytes)."""
    author = entry.get("author") or {}
    return {
        **entry,
        "source": "community",
        # The console shows one "publisher" column for both halves of the
        # store; for a submission that is the person who wrote it.
        "publisher": author.get("username") or "",
    }


def _matches(entry: dict, needle: str) -> bool:
    haystack = " ".join(str(entry.get(field) or "") for field in (
        "catalog_id", "name", "title", "description", "publisher",
    ))
    return needle.lower() in haystack.lower()


def _sort_entries(entries: list[dict], sort: str) -> list[dict]:
    """Order the merged list the way the requested column reads.

    Catalogue entries have no submission date — they were shipped, not
    submitted — so any date ordering puts them after everything dated rather
    than letting a missing value pass for "oldest".
    """
    def by_name(entry: dict) -> str:
        return (entry.get("title") or entry.get("name") or "").lower()

    if sort == "name":
        return sorted(entries, key=by_name)
    if sort == "installs":
        return sorted(entries, key=lambda e: (-int(e.get("installs_count") or 0), by_name(e)))
    dated = sorted((e for e in entries if e.get("published_at")), key=by_name)
    # Stable, so equal timestamps keep the name order established above.
    dated.sort(key=lambda e: e["published_at"], reverse=(sort == "recent"))
    return dated + sorted((e for e in entries if not e.get("published_at")), key=by_name)


@router.get("/store")
async def list_store(
    request: Request,
    origin: str | None = Query(None, pattern=f"^({ORIGINS})$"),
    kind: str | None = Query(None, pattern=f"^({KINDS})$"),
    listing: str | None = Query(None, pattern=f"^({LISTINGS})$"),
    q: str = "",
    sort: str = Query("recent", pattern=f"^({SORTS})$"),
    offset: int = Query(0, ge=0, le=MAX_STORE_OFFSET),
    limit: int = Query(50, ge=1, le=200),
    admin: dict = Depends(require_admin),
):
    """Both halves of the store in one filterable, paginated list."""
    needle = q.strip()
    entries: list[dict] = []
    catalog_total = 0
    # A submission always has an author, so "third_party" excludes them; the
    # catalogue ships no user content, so "community" excludes it.
    if origin != "community":
        catalogue = await load_catalog()
        matched = [
            entry
            for group in (catalogue["skills"], catalogue["mcp"])
            for entry in group
            if (kind is None or entry.get("kind") == kind)
            and (origin is None or entry.get("origin") == origin)
            and (listing is None or entry.get("listing") == listing)
            and (not needle or _matches(entry, needle))
        ]
        counts = await _install_counts([e.get("catalog_id") or "" for e in matched])
        entries.extend(_catalog_row(entry, counts) for entry in matched)
        catalog_total = len(matched)

    community_total = 0
    if kind != "mcp" and origin != "third_party":
        official = {"official": True, "community": False}.get(origin or "")
        window = offset + limit
        fetched: list[dict] = []
        while True:
            # Only the requested window is read: a merged page can never need
            # more than `offset + limit` rows from either source, and
            # MAX_STORE_OFFSET bounds how many windows that can be. The count
            # is asked for once — the filters do not change between windows, so
            # every later one would return the same number at the same price.
            page = await user_library.list_all_store_entries(
                listing=listing,
                official=official,
                query=needle or None,
                sort=sort,
                offset=len(fetched),
                limit=MERGE_PAGE,
                with_total=not fetched,
            )
            if not fetched:
                community_total = page["total"] or 0
            fetched.extend(page["entries"])
            if (
                len(fetched) >= window
                or len(fetched) >= community_total
                or not page["entries"]
            ):
                break
        entries.extend(_community_row(entry) for entry in fetched)

    items = _sort_entries(entries, sort)[offset:offset + limit]
    await record(
        admin["user_id"], admin.get("workspace_id"), "admin.view_skills",
        "user_skill", None,
        {"origin": origin, "kind": kind, "listing": listing, "q": q,
         "sort": sort, "offset": offset, "limit": limit},
        request,
    )
    return {
        "items": items,
        "total": catalog_total + community_total,
        "offset": offset,
        "limit": limit,
    }


@router.post("/store/{catalog_id}/listing")
async def set_listing(
    catalog_id: str,
    body: ListingBody,
    request: Request,
    admin: dict = Depends(require_admin),
):
    """Put an entry on or off the shelf, or resolve a submission."""
    note = _note(
        body.note,
        required=body.listing in (
            user_library.LISTING_DELISTED, user_library.LISTING_REJECTED,
        ),
    )
    try:
        result = await user_library.set_listing(
            catalog_id, body.listing, note=note, actor_user_id=admin["user_id"],
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return await _settle(
        result, action="admin.skill.listing", admin=admin, request=request,
        detail={"listing": body.listing, "note": note},
    )


@router.post("/store/{catalog_id}/featured")
async def set_featured(
    catalog_id: str,
    body: FeaturedBody,
    request: Request,
    admin: dict = Depends(require_admin),
):
    """Pin an entry to the top of its section, or unpin it."""
    try:
        result = await user_library.set_featured(
            catalog_id, body.featured, actor_user_id=admin["user_id"],
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return await _settle(
        result, action="admin.skill.featured", admin=admin, request=request,
        detail={"featured": body.featured},
    )


@router.post("/store/{catalog_id}/official")
async def set_official(
    catalog_id: str,
    body: OfficialBody,
    request: Request,
    admin: dict = Depends(require_admin),
):
    """Move a submission between the official and community shelves."""
    try:
        result = await user_library.set_official(
            catalog_id, body.is_official, actor_user_id=admin["user_id"],
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return await _settle(
        result, action="admin.skill.official", admin=admin, request=request,
        detail={"is_official": body.is_official},
    )


# ── Review queue ───────────────────────────────────────────────────────────

@router.get("/review")
async def list_review_queue(
    request: Request,
    state: str = Query("pending", pattern=f"^({REVIEW_STATES})$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    admin: dict = Depends(require_admin),
):
    """Submissions awaiting a verdict, longest wait first."""
    page = await user_library.list_all_store_entries(
        listing=state,
        status=user_library.PUBLISHED,
        sort="oldest",
        offset=offset,
        limit=limit,
    )
    await record(
        admin["user_id"], admin.get("workspace_id"), "admin.view_skills",
        "user_skill", None,
        {"state": state, "offset": offset, "limit": limit}, request,
    )
    return {
        "items": [_community_row(entry) for entry in page["entries"]],
        "total": page["total"],
        "offset": offset,
        "limit": limit,
    }


def _skill_md_member(infos: list[zipfile.ZipInfo]) -> zipfile.ZipInfo | None:
    """The shallowest SKILL.md in the archive.

    Packages arrive both flat and wrapped in a single directory, and a deeper
    copy (a bundled example, or a decoy) must not shadow the real manifest.
    """
    candidates = [
        info for info in infos
        if not info.is_dir() and info.filename.rsplit("/", 1)[-1].lower() == "skill.md"
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda info: (info.filename.count("/"), info.filename))


def _inspect_archive(data: bytes | None) -> dict:
    """Read a submission's manifest without trusting any of it.

    Nothing is extracted to disk and nothing is executed; the only decompressed
    read is a capped one of SKILL.md, so a member that declares — or is — a
    gigabyte still costs 64 KB to review. Declared member sizes are reported
    verbatim because they are the author's claim about their own package, and
    the reviewer is exactly the person who should see an implausible one.
    """
    result: dict = {
        "files": [],
        "files_total": 0,
        "files_truncated": False,
        "skill_md": None,
        "skill_md_truncated": False,
        "skill_md_error": None,
        "archive_error": None,
    }
    if not data:
        result["archive_error"] = "This submission has no archive"
        return result
    if len(data) > MAX_ARCHIVE_BYTES:
        result["archive_error"] = (
            f"Archive is {len(data)} bytes; this view refuses to open anything "
            f"over {MAX_ARCHIVE_BYTES} bytes"
        )
        return result
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                result["files_total"] = len(infos)
                result["archive_error"] = (
                    f"Archive declares {len(infos)} members, far past anything a "
                    f"skill package needs; refusing to list it"
                )
                return result
            # Directory records are not files and would make the count read as
            # a truncation that never happened.
            members = [info for info in infos if not info.is_dir()]
            result["files_total"] = len(members)
            result["files_truncated"] = len(members) > MAX_LISTED_FILES
            result["files"] = [
                {"path": info.filename, "size": info.file_size}
                for info in members[:MAX_LISTED_FILES]
            ]
            member = _skill_md_member(infos)
            if member is None:
                result["skill_md_error"] = "No SKILL.md in this archive"
                return result
            with archive.open(member) as handle:
                raw = handle.read(SKILL_MD_LIMIT + 1)
            result["skill_md_truncated"] = len(raw) > SKILL_MD_LIMIT
            # Replacement characters, not an error: a reviewer reading a
            # mis-encoded manifest is better served by seeing it.
            result["skill_md"] = raw[:SKILL_MD_LIMIT].decode("utf-8", "replace")
    except Exception as exc:
        result["archive_error"] = f"Could not read this archive: {type(exc).__name__}"
    return result


async def _submission(catalog_id: str) -> dict:
    """Resolve a submission for review in any listing state, or 404."""
    if not _is_community(catalog_id):
        raise HTTPException(404, detail="Only a submission has a review record")
    try:
        row = await user_library.get_published_skill(
            catalog_id, include_archive=True, require_listed=False,
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    if not row:
        raise HTTPException(404, detail="Submission not found")
    return row


@router.get("/review/{catalog_id}")
async def get_review_detail(
    catalog_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
):
    """Everything a verdict rests on: metadata, manifest and file list."""
    row = await _submission(catalog_id)
    async with get_db_session() as session:
        author = (await session.execute(
            select(User).where(User.id == row["owner_id"])
        )).scalar_one_or_none()
        installs = await session.scalar(
            select(func.count(SkillInstall.id))
            .where(SkillInstall.catalog_id == row["catalog_id"])
        )
    archive = _inspect_archive(row.get("archive_data"))
    await record(
        admin["user_id"], row.get("workspace_id"), "admin.view_skills",
        "user_skill", row["catalog_id"], {"files": archive["files_total"]}, request,
    )
    return {
        "catalog_id": row["catalog_id"],
        "library_id": row["library_id"],
        "kind": "skill",
        "source": "community",
        "origin": row["origin"],
        "name": row["name"],
        "title": row["title"],
        "icon": row["icon"],
        "description": row["description"],
        "install_dir": row["install_dir"],
        "author": {
            "user_id": row["owner_id"],
            "username": author.username if author else None,
            "email": author.email if author else None,
        },
        "workspace_id": row["workspace_id"],
        "version": row["version"],
        "status": row["status"],
        "listing": row["listing"],
        "listing_note": row["listing_note"],
        "featured": row["featured"],
        "is_official": row["is_official"],
        "requires_mcp": row["requires_mcp"],
        "homepage": row["homepage"],
        "size": row["archive_size"],
        "sha256": row.get("archive_sha256"),
        "installs_count": int(installs or 0),
        "created_at": row["created_at"],
        "published_at": row["published_at"],
        **archive,
    }


@router.get("/review/{catalog_id}/archive")
async def download_review_archive(
    catalog_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
):
    """Download the exact bytes a reviewer is being asked to approve."""
    row = await _submission(catalog_id)
    data = row.get("archive_data")
    if not data:
        raise HTTPException(404, detail="This submission has no archive")
    filename = f"{row['name']}.zip"
    disposition = (
        f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
    )
    await record(
        admin["user_id"], row.get("workspace_id"), "admin.skill.download",
        "user_skill", row["catalog_id"], {"size": len(data)}, request,
    )
    return StreamingResponse(
        BytesIO(data),
        media_type="application/zip",
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(len(data)),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/review/{catalog_id}/approve")
async def approve_submission(
    catalog_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
):
    """Approve a submission: it goes on the shelf and the author is told."""
    if not _is_community(catalog_id):
        raise HTTPException(404, detail="Only a submission can be approved")
    try:
        result = await user_library.set_listing(
            catalog_id, user_library.LISTING_LISTED, actor_user_id=admin["user_id"],
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return await _settle(
        result, action="admin.skill.approve", admin=admin, request=request, detail={},
    )


@router.post("/review/{catalog_id}/reject")
async def reject_submission(
    catalog_id: str,
    body: NoteBody,
    request: Request,
    admin: dict = Depends(require_admin),
):
    """Reject a submission. The reason is mandatory — the author only sees it."""
    if not _is_community(catalog_id):
        raise HTTPException(404, detail="Only a submission can be rejected")
    note = _note(body.note, required=True)
    try:
        result = await user_library.set_listing(
            catalog_id, user_library.LISTING_REJECTED,
            note=note, actor_user_id=admin["user_id"],
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return await _settle(
        result, action="admin.skill.reject", admin=admin, request=request,
        detail={"note": note},
    )


# ── Installs ───────────────────────────────────────────────────────────────

@router.get("/installs")
async def list_installs(
    request: Request,
    catalog_id: str | None = None,
    user_id: str | None = None,
    q: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    admin: dict = Depends(require_admin),
):
    """Who installed what, from either half of the store.

    Only named columns of ``user_skills`` are selected, so a page of installs
    never drags a published archive through the connection.
    """
    stmt = (
        select(
            SkillInstall,
            User,
            UserSkill.published_name,
            UserSkill.published_icon,
            UserSkill.is_official,
        )
        .join(User, User.id == SkillInstall.user_id)
        # Catalogue installs have no user_skills row behind them at all.
        .outerjoin(UserSkill, UserSkill.id == SkillInstall.user_skill_id)
    )
    if catalog_id:
        stmt = stmt.where(SkillInstall.catalog_id == catalog_id)
    if user_id:
        stmt = stmt.where(SkillInstall.user_id == user_id)
    needle = q.strip()
    if needle:
        pattern = f"%{needle}%"
        stmt = stmt.where(or_(
            User.username.ilike(pattern),
            User.email.ilike(pattern),
            SkillInstall.name.ilike(pattern),
            UserSkill.published_name.ilike(pattern),
        ))

    index = catalog_index()
    async with get_db_session() as session:
        total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (await session.execute(
            stmt.order_by(SkillInstall.installed_at.desc(), SkillInstall.id.desc())
            .offset(offset).limit(limit)
        )).all()

    items = []
    for install, user, published_name, published_icon, is_official in rows:
        entry = index.get(install.catalog_id) or {}
        if install.catalog_id.startswith(user_library.COMMUNITY_PREFIX):
            origin = "official" if is_official else "community"
        else:
            origin = catalog_entry_origin(entry)
        items.append({
            "id": install.id,
            "user": {"id": user.id, "username": user.username, "email": user.email},
            "catalog_id": install.catalog_id,
            "kind": install.kind,
            "origin": origin,
            "name": install.name,
            "title": published_name or entry.get("title") or install.name,
            "icon": published_icon or entry.get("icon") or "",
            "install_dir": install.install_dir,
            "installed_at": _at(install.installed_at),
        })
    await record(
        admin["user_id"], admin.get("workspace_id"), "admin.view_skills",
        "skill_install", catalog_id,
        {"catalog_id": catalog_id, "user_id": user_id, "q": q,
         "offset": offset, "limit": limit},
        request,
    )
    return {"items": items, "total": int(total or 0), "offset": offset, "limit": limit}
