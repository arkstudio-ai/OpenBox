"""Portable install labels, including compatibility with older Action Servers.

Only the management presentation uses this adapter. Model discovery and Skill
references continue to use their original name and description.
"""
import asyncio
from collections import Counter
import json
from pathlib import PurePosixPath
import re
import time

from core.markdown import parse_frontmatter
from skill.display import DISPLAY_FILE, DISPLAY_MAX_BYTES, display_fields, with_package_display


def _install_root(client, row: dict) -> PurePosixPath:
    name = row.get("install_dir")
    if (row.get("source") != "container" or not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}", name)):
        raise ValueError("Display metadata requires a user-installed Skill directory")
    base = PurePosixPath(str(row.get("base_dir") or ""))
    roots = [PurePosixPath("/data/skills") / name]
    scope = getattr(client, "user_scope", "")
    if isinstance(scope, str) and re.fullmatch(r"u-[a-f0-9]{20}", scope):
        roots.insert(0, PurePosixPath("/data/skills") / scope / name)
    for root in roots:
        if ".." not in base.parts and base.is_relative_to(root):
            return root
    raise ValueError("Skill directory is outside this user's install roots")


async def save_install_display(client, installed: dict, fields: dict) -> dict:
    fields = display_fields(fields, strict=True)
    if not fields:
        return installed
    name = installed.get("install_dir") or installed.get("name")
    info = await client.get_skill(name)
    root = _install_root(client, info)
    # A regular resource file survives existing ZIP export/import on old servers.
    try:
        try:
            existing = display_fields(json.loads(await client.read_file_raw(
                str(root / DISPLAY_FILE), max_bytes=DISPLAY_MAX_BYTES
            )))
        except (FileNotFoundError, ValueError):
            existing = {}
        merged = dict(existing)
        for key, translations in fields.items():
            merged[key] = {**existing.get(key, {}), **translations}
        await client.write_file(str(root / DISPLAY_FILE), json.dumps(merged, ensure_ascii=False, indent=2))
    except Exception as exc:
        raise RuntimeError("Skill installed, but its display copy could not be saved. Check the installed copy before retrying.") from exc
    client._invalidate_catalogue_cache()
    client._skill_display_cache = {}
    return {**installed, **merged}


async def enrich_install_display(client, rows: list[dict], *, package_as_skill: bool = True) -> list[dict]:
    """Read legacy display data once per content revision, with a bounded TTL."""
    counts = Counter(row.get("install_dir") or row.get("name") for row in rows)
    cache = getattr(client, "_skill_display_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        client._skill_display_cache = cache
    if len(cache) > 512:
        cache.clear()
    semaphore = asyncio.Semaphore(4)

    async def enrich(row):
        if row.get("source") != "container" or row.get("display_metadata_version") == 1:
            return row
        single = package_as_skill and counts[row.get("install_dir") or row.get("name")] == 1
        key = (row.get("install_dir"), row.get("name"), row.get("package_digest"), single)
        now = time.monotonic()
        cached = cache.get(key)
        if cached and now - cached[0] < 30:
            return {**row, **cached[1]}
        async with semaphore:
            fields = {}
            try:
                info = row if row.get("content") else await client._get(f"/skills/{row['name']}")
                metadata, _ = parse_frontmatter(info.get("content") or "")
                fields = display_fields(metadata)
                root = _install_root(client, info)
                try:
                    raw = await client.read_file_raw(str(root / DISPLAY_FILE), max_bytes=DISPLAY_MAX_BYTES)
                    package = json.loads(raw)
                    fields = with_package_display(fields, package, single=single)
                except (FileNotFoundError, ValueError):
                    pass
                except Exception:
                    # Missing sidecars on legacy servers must not hide Skills.
                    pass
                cache[key] = (now, fields)
            except Exception:
                # Connection failures are not cached as authoritative empty copy.
                pass
            return {**row, **fields}

    return list(await asyncio.gather(*(enrich(row) for row in rows)))
