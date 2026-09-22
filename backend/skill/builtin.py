"""The single, cwd-independent inventory of first-party Skill packages.

The manifest classifies instruction packages, not tool permissions or service
health. Personal/project Skills and the installable store catalogue stay in
their own providers. Browser runtime code is assembled separately from its
instruction package.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from skill.display import display_fields

BUILTIN_ROOT = Path(__file__).resolve().with_name("builtins")
_IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


@dataclass(frozen=True)
class BuiltinSkill:
    name: str
    group: str
    directory: Path
    group_title: dict[str, str]
    display_name: dict[str, str]
    display_description: dict[str, str]


def builtin_skills() -> tuple[BuiltinSkill, ...]:
    """Read only declared packages; validate identity before exposing any."""
    manifest = json.loads((BUILTIN_ROOT / "catalog.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("Unsupported builtin Skill catalog version")
    group_rows = manifest.get("groups")
    skill_rows = manifest.get("skills")
    if not isinstance(group_rows, list) or not group_rows:
        raise ValueError("Builtin Skill catalog needs a list of groups")
    if not isinstance(skill_rows, list) or not skill_rows:
        raise ValueError("Builtin Skill catalog needs a list of skills")
    groups: dict[str, dict[str, str]] = {}
    for row in group_rows:
        if not isinstance(row, dict):
            raise ValueError("Builtin Skill group must be an object")
        group, title = row.get("id"), row.get("title")
        if not isinstance(group, str) or not _IDENTIFIER.fullmatch(group) or group in groups:
            raise ValueError("Builtin Skill groups must have unique, safe identifiers")
        if not isinstance(title, dict) or not all(
            isinstance(title.get(lang), str) and title[lang].strip()
            for lang in ("zh-CN", "en-US")
        ):
            raise ValueError(f"Builtin Skill group labels are missing: {group}")
        groups[group] = title
    specs = []
    names = set()
    for row in skill_rows:
        if not isinstance(row, dict):
            raise ValueError("Builtin Skill entry must be an object")
        name, group = row.get("name", ""), row.get("group", "")
        if (not isinstance(name, str) or not _IDENTIFIER.fullmatch(name)
                or not isinstance(group, str) or group not in groups):
            raise ValueError("Builtin Skill has an invalid name or group")
        if name in names:
            raise ValueError(f"Duplicate builtin Skill: {name}")
        directory = BUILTIN_ROOT / group / name
        if not (directory / "SKILL.md").is_file() or directory.resolve().parent.parent != BUILTIN_ROOT.resolve():
            raise ValueError(f"Builtin Skill package missing or outside catalog: {name}")
        display = display_fields(row, strict=True)
        specs.append(BuiltinSkill(name, group, directory, groups[group],
                                  display.get("display_name", {}), display.get("display_description", {})))
        names.add(name)
    return tuple(specs)


def builtin_directory(name: str) -> Path:
    for spec in builtin_skills():
        if spec.name == name:
            return spec.directory
    raise KeyError(f"Unregistered builtin Skill: {name}")


def builtin_directories() -> tuple[Path, ...]:
    return tuple(spec.directory for spec in builtin_skills())


def builtin_names() -> frozenset[str]:
    return frozenset(spec.name for spec in builtin_skills())


def validate_catalog() -> list[dict]:
    """Development/build gate: every package is registered once and loadable."""
    from core.markdown import parse_frontmatter

    specs = builtin_skills()
    declared = {spec.directory / "SKILL.md" for spec in specs}
    actual = set(BUILTIN_ROOT.rglob("SKILL.md"))
    if declared != actual:
        raise ValueError(f"Unregistered builtin Skill packages: {sorted(str(p) for p in actual - declared)}")
    inventory = []
    for spec in specs:
        metadata, body = parse_frontmatter((spec.directory / "SKILL.md").read_text(encoding="utf-8"))
        if metadata.get("name") != spec.name or not metadata.get("description") or not body.strip():
            raise ValueError(f"Builtin Skill metadata does not match catalog: {spec.name}")
        inventory.append({"name": spec.name, "group": spec.group, "path": str(spec.directory)})
    return inventory


if __name__ == "__main__":
    print(json.dumps(validate_catalog(), ensure_ascii=False, indent=2))
