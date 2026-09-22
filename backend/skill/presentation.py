"""Shared host/desktop selection for legacy listings and management APIs."""
from __future__ import annotations

from skill.builtin import builtin_names
from skill.display import display_fields


def skill_row(skill) -> dict:
    row = {
        "name": skill.name,
        "description": skill.description,
        "source": skill.source,
        "allowed_tools": list(getattr(skill, "allowed_tools", ())),
        "requires_mcp": list(getattr(skill, "requires_mcp", ())),
    }
    if getattr(skill, "builtin_group", ""):
        row["builtin_group"] = skill.builtin_group
        row["builtin_group_title"] = dict(skill.builtin_group_title)
    row.update(display_fields({"display_name": getattr(skill, "display_name", {}),
                               "display_description": getattr(skill, "display_description", {})}))
    return row


def host_overrides_remote(host, remote: dict) -> bool:
    return host.source == "project" or (
        remote.get("source") == "builtin" and host.name in builtin_names()
    )


def merge_skill_listings(remote: list[dict], host) -> list[dict]:
    """Project overrides > user-installed desktop > global host > builtins.

    Old image-baked copies of known builtins defer to backend packages. Unknown
    remote builtins stay visible. Provenance and uninstall keys of user packages
    are retained, not copied onto a same-name system package.
    """
    merged = {row["name"]: dict(row) for row in remote if isinstance(row, dict) and row.get("name")}
    for skill in host:
        previous = merged.get(skill.name)
        if previous is None or host_overrides_remote(skill, previous):
            merged[skill.name] = skill_row(skill)
    return list(merged.values())
