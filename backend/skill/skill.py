"""Skill discovery and loading from SKILL.md files."""
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.markdown import parse_frontmatter, clip_description
from skill.builtin import builtin_directories, builtin_skills
from skill.display import display_fields
from core.log import create_logger
from skill.provider import (
    ScopeKey,
    SkillCatalogSnapshot,
    SkillCatalogueUnavailable,
    SkillDefinition as ProviderSkillDefinition,
    SkillDiagnostic,
    SkillProvider,
    SkillProviderSnapshot,
    SkillRegistry,
    SkillScopeMismatch,
    SkillSnapshotStale,
    create_default_skill_registry,
)

log = create_logger("skill")


@dataclass
class SkillInfo:
    name: str
    description: str
    source: str  # "builtin", "global" or "project"
    content: str
    # Directory holding SKILL.md, on the machine running the backend. Note this
    # is NOT reachable from the agent's tools, which execute in the sandbox.
    path: str = ""
    # Documentary/display-only names describing tools a skill discusses.
    # Skill fields never affect the runtime tool set; exposure is owned by the
    # agent allowlist and permission rules (decoupled 2026-08-30).
    allowed_tools: tuple[str, ...] = ()
    builtin_group: str = ""
    builtin_group_title: dict[str, str] = field(default_factory=dict)
    display_name: dict[str, str] = field(default_factory=dict)
    display_description: dict[str, str] = field(default_factory=dict)


# Cache
_skills: dict[str, SkillInfo] = {}
_loaded = False
# Fingerprint of what the last scan saw, so an edit on disk is noticed. The
# server is long-lived; without this, adding or editing a skill did nothing
# until a restart, and the description shipped to the model stayed stale.
_fingerprint: tuple = ()
# Content checks also catch edits that preserve size and timestamps (for
# example a synced Docker volume). Bound their frequency on long-lived servers.
_CHECK_INTERVAL_SECONDS = 2.0
_last_check = 0.0


def _skill_dirs() -> list[Path]:
    """Legacy global/project roots; system packages come from builtin.py."""
    config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    cwd = Path.cwd()
    return [Path(config_home) / n / "skills" for n in ("openbox", "openagent")] + [
        cwd / ".openbox" / "skills",
        cwd / ".openagent" / "skills",
        cwd / ".claude" / "skills",
        cwd / ".agents" / "skills",
    ]


def _current_fingerprint() -> tuple:
    """Content signature of the skills on disk, including additions/deletions.

    mtime alone can stay unchanged across rapid writes or file synchronization.
    """
    entries = []
    for base in (*builtin_directories(), *_skill_dirs()):
        try:
            if not base.exists():
                continue
            entries.append((str(base), base.stat().st_mtime_ns))
            for md in base.rglob("SKILL.md"):
                with md.open("rb") as content:
                    entries.append((str(md), hashlib.file_digest(content, "sha256").hexdigest()))
                entries.append((str(md.parent), md.parent.stat().st_mtime_ns))
        except OSError:
            # A directory that vanished mid-scan just contributes nothing.
            continue
    return tuple(sorted(set(entries)))


async def _ensure_fresh() -> None:
    """Reload if the skills on disk no longer match what is cached."""
    global _last_check
    if not _loaded:
        await load_skills()
        return
    now = time.monotonic()
    if now - _last_check < _CHECK_INTERVAL_SECONDS:
        return
    _last_check = now
    try:
        current = _current_fingerprint()
    except Exception as e:
        log.debug(f"Could not fingerprint skills, keeping cache: {e}")
        return
    if current != _fingerprint:
        log.info("Skills changed on disk, reloading")
        await load_skills()


def _scan_directory(base_dir: Path, source: str) -> list[SkillInfo]:
    """Scan a directory for SKILL.md files."""
    results = []
    if not base_dir.exists():
        return results

    for skill_md in base_dir.rglob("SKILL.md"):
        try:
            content = skill_md.read_text(encoding="utf-8")
            metadata, body = parse_frontmatter(content)

            name = metadata.get("name", skill_md.parent.name)
            # Clipped at the source: the description is advertised on every
            # request, and a frontmatter that has grown into prose costs tokens
            # on all of them. The full text is in the body, which the skill tool
            # returns when the model actually loads it.
            description = clip_description(metadata.get("description", ""))
            allowed_tools = normalize_skill_tools(
                metadata.get("allowed-tools")
                or metadata.get("allowed_tools")
                or metadata.get("tools")
            )
            results.append(SkillInfo(
                name=name,
                description=description,
                source=source,
                content=body,
                path=str(skill_md.parent),
                allowed_tools=allowed_tools,
                **display_fields(metadata),
            ))
        except Exception as e:
            log.warning(f"Failed to load skill from {skill_md}: {e}")

    return results


def normalize_skill_tools(value) -> tuple[str, ...]:
    """Normalize a documentary ``allowed-tools`` declaration for display."""
    if isinstance(value, str):
        values = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        return ()
    out: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name and len(name) <= 128 and not any(ord(char) < 32 or ord(char) == 127 for char in name) and name not in out:
            out.append(name)
        if len(out) >= 64:
            break
    return tuple(out)


async def load_skills() -> None:
    """Load all available skills."""
    global _skills, _loaded, _fingerprint, _last_check
    # Capture before reading definitions. An edit during the scan must leave
    # the cache stale for the next check, not stamp old text with its new hash.
    try:
        _fingerprint = _current_fingerprint()
    except Exception:
        _fingerprint = ()
    _skills.clear()

    for spec in builtin_skills():
        for skill in _scan_directory(spec.directory, "builtin"):
            if skill.name != spec.name:
                raise ValueError(f"Builtin Skill identity mismatch: {spec.name}")
            skill.builtin_group = spec.group
            skill.builtin_group_title = spec.group_title
            skill.display_name = spec.display_name
            skill.display_description = spec.display_description
            _skills[skill.name] = skill

    globals_, projects = _skill_dirs()[:2], _skill_dirs()[2:]
    for global_dir in globals_:
        for skill in _scan_directory(global_dir, "global"):
            _skills[skill.name] = skill
    for skills_dir in projects:
        for skill in _scan_directory(skills_dir, "project"):
            _skills[skill.name] = skill

    _loaded = True
    _last_check = time.monotonic()
    log.info(f"Loaded {len(_skills)} skills")


def _provider_info(skill: ProviderSkillDefinition) -> SkillInfo:
    return SkillInfo(
        name=skill.name,
        description=skill.description,
        source=skill.source,
        content=skill.content,
        path=skill.path or skill.base_dir,
        allowed_tools=skill.allowed_tools,
        builtin_group=str(skill.metadata.get("builtin_group") or ""),
        builtin_group_title=dict(skill.metadata.get("builtin_group_title") or {}),
        **display_fields(skill.metadata),
    )


async def get_skill(
    name: str,
    *,
    scope: ScopeKey | None = None,
    registry: SkillRegistry | None = None,
    snapshot: SkillCatalogSnapshot | None = None,
) -> SkillInfo | None:
    """Get a skill by name.

    The keyword-only scoped form is the Agent/session API.  Calling without a
    scope preserves the historical host-only helper for management endpoints
    and older integrations; Agent code must never use that implicit cwd path.
    """
    if scope is not None:
        owned_registry = registry is None
        active = registry or create_default_skill_registry(None)
        try:
            selected = snapshot or await active.snapshot(scope)
            definition = await active.load(selected, name, scope=scope)
            return _provider_info(definition) if definition is not None else None
        finally:
            if owned_registry:
                await active.dispose()
    await _ensure_fresh()
    return _skills.get(name)


async def list_skills(
    *,
    scope: ScopeKey | None = None,
    registry: SkillRegistry | None = None,
) -> list[SkillInfo]:
    """List all available skills, optionally through the scoped registry."""
    if scope is not None:
        owned_registry = registry is None
        active = registry or create_default_skill_registry(None)
        try:
            return [_provider_info(skill) for skill in await active.list(scope)]
        finally:
            if owned_registry:
                await active.dispose()
    await _ensure_fresh()
    return list(_skills.values())
