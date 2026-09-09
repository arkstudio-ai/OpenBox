"""Operator-owned store overlays. Never mutate an author's private snapshot."""

import asyncio
import json
from datetime import datetime, timezone
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import urlsplit
import zipfile

from sqlalchemy import select
from sqlalchemy.orm import undefer

from db.base import get_db_session
from db.models.catalog_override import CatalogOverride
from db.models.skill_catalog_package import SkillCatalogPackage
from skill.package_validation import (
    manifest_metadata,
    prepare_zip,
    skill_slug,
    zip_manifest,
)


def not_deleted(key):
    return (
        ~select(SkillCatalogPackage.catalog_id)
        .where(
            SkillCatalogPackage.catalog_id == key,
            SkillCatalogPackage.deleted.is_(True),
        )
        .exists()
    )


async def overlays() -> dict[str, dict]:
    async with get_db_session() as session:
        rows = (await session.execute(select(SkillCatalogPackage))).scalars().all()
        return {r.catalog_id: {**r.definition, "deleted": r.deleted} for r in rows}


async def merge_catalog(
    skills: list[dict], mcp: list[dict]
) -> tuple[list[dict], list[dict]]:
    data = await overlays()
    result = []
    for kind, entries in (("skill", skills), ("mcp", mcp)):
        keyed = {f"{kind}:{e['id']}": dict(e) for e in entries}
        for key, value in data.items():
            if not key.startswith(kind + ":"):
                continue
            if value["deleted"]:
                keyed.pop(key, None)
            elif key in keyed or value.get("id"):
                keyed[key] = {**keyed.get(key, {}), **value}
        result.append(list(keyed.values()))
    return result[0], result[1]


async def dependency_index(base: dict[str, dict]) -> dict[str, dict]:
    """Shelf hiding is allowed for dependencies; deletion and edits still apply."""
    result = dict(base)
    for key, value in (await overlays()).items():
        if not key.startswith("mcp:"):
            continue
        if value["deleted"]:
            result.pop(key, None)
        elif key in result or value.get("id"):
            result[key] = {**result.get(key, {}), **value}
    return result


async def apply_metadata(
    entries: list[dict], *, include_archive: bool = False
) -> list[dict]:
    if not entries:
        return []
    ids = [e["catalog_id"] for e in entries]
    async with get_db_session() as session:
        stmt = select(SkillCatalogPackage).where(
            SkillCatalogPackage.catalog_id.in_(ids)
        )
        if include_archive:
            stmt = stmt.options(undefer(SkillCatalogPackage.archive_data))
        rows = {r.catalog_id: r for r in (await session.execute(stmt)).scalars()}
        result = []
        for entry in entries:
            row = rows.get(entry["catalog_id"])
            if row and row.deleted:
                continue
            item = {**entry, **(row.definition if row else {})}
            if row and include_archive and row.archive_data is not None:
                item["archive_data"] = row.archive_data
                item["archive_sha256"] = row.definition["sha256"]
            result.append(item)
        return result


async def archive_bytes(catalog_id: str) -> bytes | None:
    async with get_db_session() as session:
        return await session.scalar(
            select(SkillCatalogPackage.archive_data).where(
                SkillCatalogPackage.catalog_id == catalog_id,
                SkillCatalogPackage.deleted.is_(False),
            )
        )


def validate_icon(icon: str) -> str:
    icon = icon.strip()
    if not icon or (len(icon) <= 16 and not any(c in icon for c in "<>:/")):
        return icon
    url = urlsplit(icon)
    if (
        len(icon) <= 2048
        and url.scheme == "https"
        and url.hostname
        and not url.username
        and not url.password
    ):
        return icon
    raise ValueError("Icon must be an emoji or an HTTPS image URL")


async def resolve_entry(catalog_id: str) -> dict:
    if catalog_id.startswith("community:"):
        from skill.user_library import get_published_skill

        entry = await get_published_skill(catalog_id, require_listed=False)
        if entry:
            return {**entry, "kind": "skill"}
    else:
        from skill.catalog import shelf_index

        entry = (await shelf_index()).get(catalog_id)
        if entry:
            return entry
    raise LookupError("Store entry not found")


def validate_mcp_config(config: dict) -> dict:
    if len(json.dumps(config, ensure_ascii=False).encode()) > 65536:
        raise ValueError("MCP configuration exceeds 64 KB")
    if config.get("type") == "stdio":
        command = config.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("MCP command is required")
        args = config.get("args", [])
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise ValueError("MCP args must be a list of strings")
    elif config.get("type") == "remote":
        url = urlsplit(str(config.get("url", "")))
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError(
                "Remote MCP requires an HTTPS URL without embedded credentials"
            )
    else:
        raise ValueError("MCP type must be stdio or remote")
    for key in ("env", "headers"):
        value = config.get(key, {})
        if not isinstance(value, dict) or any(
            not isinstance(v, str) for v in value.values()
        ):
            raise ValueError(f"MCP {key} must be a string-to-string mapping")
    if "timeout" in config and (
        type(config["timeout"]) not in (int, float) or not 0 < config["timeout"] <= 600
    ):
        raise ValueError("MCP timeout must be between 0 and 600 seconds")
    return config


async def detail(catalog_id: str) -> dict:
    entry = await resolve_entry(catalog_id)
    data = await archive_bytes(catalog_id)
    if data is None and catalog_id.startswith("community:"):
        from skill.user_library import get_published_skill

        original = await get_published_skill(
            catalog_id, include_archive=True, require_listed=False
        )
        data = original.get("archive_data") if original else None
    content = (entry.get("install") or {}).get("content", "")
    if data:
        # Legacy community ZIPs may not satisfy today's upload policy. The
        # existing review download remains available; don't open unsafe bytes.
        content = await asyncio.to_thread(_read_content, data)
    return {**entry, "content": content, "revision": entry.get("revision", 0)}


def _read_content(data: bytes) -> str:
    try:
        cleaned, _ = prepare_zip(data)
        with zipfile.ZipFile(BytesIO(cleaned)) as archive:
            name = next(
                n for n in archive.namelist() if PurePosixPath(n).name == "SKILL.md"
            )
            return archive.read(name).decode("utf-8")
    except ValueError:
        return ""


def _replace_content(old: bytes | None, name: str, content: str) -> tuple[bytes, dict]:
    if not old:
        return prepare_zip(zip_manifest(name, content))
    old, _ = prepare_zip(old)
    buffer = BytesIO()
    with (
        zipfile.ZipFile(BytesIO(old)) as source,
        zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for member in source.infolist():
            target.writestr(
                member,
                content.encode()
                if PurePosixPath(member.filename).name == "SKILL.md"
                else source.read(member),
            )
    return prepare_zip(buffer.getvalue())


async def create_entry(values: dict, actor: str, archive: bytes | None = None) -> dict:
    from skill.catalog import shelf_index

    name = skill_slug(values["name"])
    kind = values.get("kind", "skill")
    key = f"{kind}:{name}"
    if key in await shelf_index():
        raise FileExistsError("This skill name already exists; edit it instead")
    icon = validate_icon(values.get("icon", ""))
    if kind == "skill":
        archive = (
            archive
            if archive is not None
            else zip_manifest(name, values.get("content", ""))
        )
        archive, package = await asyncio.to_thread(prepare_zip, archive)
        if package["name"] != name:
            raise ValueError("ZIP name must match the skill name")
    else:
        package = {}
        validate_mcp_config(values.get("config") or {})
    now = datetime.now(timezone.utc)
    definition = {
        **package,
        "id": name,
        "catalog_id": key,
        "kind": kind,
        "name": name,
        "title": values.get("title") or name,
        "description": values.get("description") or package.get("description", ""),
        "icon": icon or package.get("icon", ""),
        "publisher": "OpenBox",
        "origin": "official",
        "official": True,
        "managed": True,
        "revision": 1,
        "version": 1,
        "published_at": now.isoformat(),
        "install": {"name": name},
        "has_archive": archive is not None,
        "requires_mcp": package.get("requires_mcp", []),
    }
    if kind == "mcp":
        definition["config"] = values["config"]
    async with get_db_session() as session:
        existing = await session.get(SkillCatalogPackage, key)
        if existing:
            raise FileExistsError(
                "Name is reserved by a deleted entry; restore it instead"
            )
        session.add(
            SkillCatalogPackage(
                catalog_id=key,
                definition=definition,
                archive_data=archive,
                deleted=False,
                changed_by=actor,
                updated_at=now,
            )
        )
        shelf = await session.get(CatalogOverride, key)
        if shelf is None:
            session.add(
                CatalogOverride(
                    catalog_id=key,
                    listing=values.get("listing", "delisted"),
                    featured=False,
                    changed_by=actor,
                    changed_at=now,
                )
            )
        else:
            shelf.listing = values.get("listing", "delisted")
    return {"catalog_id": key, "revision": 1}


async def update_entry(
    key: str, values: dict, actor: str, expected_revision: int
) -> dict:
    entry = await resolve_entry(key)
    changes = {k: v for k, v in values.items() if k in {"title", "description", "icon"}}
    if "icon" in changes:
        changes["icon"] = validate_icon(changes["icon"])
    archive = None
    content = values.get("content")
    if content is not None:
        if entry.get("kind") != "skill":
            raise ValueError("Only skills have SKILL.md content")
        if manifest_metadata(content)["name"] != entry["name"]:
            raise ValueError("Editing must not change the skill's stable name")
        old = await archive_bytes(key)
        if old is None and key.startswith("community:"):
            from skill.user_library import get_published_skill

            release = await get_published_skill(
                key, include_archive=True, require_listed=False
            )
            old = release.get("archive_data") if release else None
        archive, package = await asyncio.to_thread(
            _replace_content, old, entry["name"], content
        )
        changes.update(
            {k: package[k] for k in ("sha256", "archive_size", "requires_mcp")}
        )
        changes.update(has_archive=True, version=int(entry.get("version") or 0) + 1)
    if "config" in values:
        if entry.get("kind") != "mcp":
            raise ValueError("Only MCP entries have server configuration")
        changes["config"] = validate_mcp_config(values["config"])
    async with get_db_session() as session:
        row = (
            await session.execute(
                select(SkillCatalogPackage)
                .where(SkillCatalogPackage.catalog_id == key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        revision = row.definition.get("revision", 0) if row else 0
        if (row and row.deleted) or revision != expected_revision:
            raise FileExistsError("Entry changed; refresh before saving")
        now = datetime.now(timezone.utc)
        if row is None:
            row = SkillCatalogPackage(
                catalog_id=key,
                definition={},
                deleted=False,
                changed_by=actor,
                updated_at=now,
            )
            session.add(row)
        row.definition = {**row.definition, **changes, "revision": revision + 1}
        row.changed_by, row.updated_at = actor, now
        if archive is not None:
            row.archive_data = archive
    return {"catalog_id": key, "revision": revision + 1}


async def delete_entry(key: str, actor: str) -> dict:
    async with get_db_session() as session:
        row = await session.get(SkillCatalogPackage, key)
        if row and row.deleted:
            return {"catalog_id": key, "changed": False}
    entry = await resolve_entry(key)
    async with get_db_session() as session:
        row = (
            await session.execute(
                select(SkillCatalogPackage)
                .where(SkillCatalogPackage.catalog_id == key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if row is None:
            row = SkillCatalogPackage(
                catalog_id=key,
                definition={},
                deleted=False,
                changed_by=actor,
                updated_at=now,
            )
            session.add(row)
        if row.deleted:
            return {"catalog_id": key, "changed": False}
        # Keep a read-only display snapshot and the authored original for recovery.
        snapshot = {
            k: v for k, v in entry.items() if k not in {"archive_data", "content"}
        }
        operator_overlay = dict(row.definition)
        row.definition = {
            **snapshot,
            **row.definition,
            "revision": row.definition.get("revision", 0) + 1,
        }
        if key.startswith("community:"):
            row.definition = {**row.definition, "_operator_overlay": operator_overlay}
        row.deleted, row.changed_by, row.updated_at = True, actor, now
    return {
        "catalog_id": key,
        "changed": True,
        "workspace_id": entry.get("workspace_id"),
    }


async def restore_entry(key: str, actor: str) -> dict:
    async with get_db_session() as session:
        row = (
            await session.execute(
                select(SkillCatalogPackage)
                .where(SkillCatalogPackage.catalog_id == key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or not row.deleted:
            raise LookupError("Deleted entry not found")
        row.deleted = False
        row.definition = {
            **row.definition,
            "revision": row.definition.get("revision", 0) + 1,
        }
        row.changed_by, row.updated_at = actor, datetime.now(timezone.utc)
        # Restore hidden, never silently publish an old/deleted release.
        if key.startswith("community:"):
            from db.models.user_skill import UserSkill

            original = await session.get(UserSkill, key.split(":", 1)[1])
            if original is None:
                raise LookupError("Original release no longer exists")
            original.listing = "delisted"
            # Do not freeze future author metadata behind a deletion snapshot.
            row.definition = {
                **row.definition.get("_operator_overlay", {}),
                "revision": row.definition["revision"],
            }
        else:
            shelf = await session.get(CatalogOverride, key)
            if shelf:
                shelf.listing = "delisted"
            else:
                session.add(
                    CatalogOverride(
                        catalog_id=key,
                        listing="delisted",
                        featured=False,
                        changed_by=actor,
                        changed_at=row.updated_at,
                    )
                )
    return {"catalog_id": key, "restored": True}
