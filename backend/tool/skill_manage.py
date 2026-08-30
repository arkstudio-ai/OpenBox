"""Authoring operations used by the skill-creator workflow."""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

from core.markdown import parse_frontmatter
from tool.tool import ToolContext, ToolResult, define_tool


_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_SKILL_MD = 128 * 1024
_MAX_FILES = 64
_MAX_TOTAL_TEXT = 2 * 1024 * 1024
_SECRET_NAMES = {
    "credentials", "credentials.json", "id_dsa", "id_ecdsa", "id_ed25519",
    "id_rsa", "password", "passwords", "secret", "secrets", "token", "tokens",
}
_SECRET_DIRS = {"credentials", "keys", "private-keys", "secrets"}
_SECRET_SUFFIXES = (".jks", ".key", ".keystore", ".p12", ".pem", ".pfx", ".secret")
_SECRET_STEMS = {
    "access-key", "api-key", "api_key", "apikey", "credential", "credentials",
    "password", "passwords", "private-key", "secret", "secrets", "service-account",
    "token", "tokens",
}


class SkillResource(BaseModel):
    path: str = Field(
        description="Relative path below the skill directory, normally scripts/, references/, assets/, or agents/"
    )
    content: str = Field(description="UTF-8 text content for this resource")


class SkillManageArgs(BaseModel):
    action: Literal["create", "export"] = Field(
        description="create writes a new personal skill; export prepares its ZIP for share_file"
    )
    name: str = Field(description="Lowercase skill slug using letters, digits, and hyphens")
    skill_md: str | None = Field(
        default=None,
        description="Complete SKILL.md including YAML frontmatter; required for create",
    )
    files: list[SkillResource] = Field(
        default_factory=list,
        description="Optional text resources bundled beside SKILL.md",
    )


def _resource_path_error(raw_path: str) -> str | None:
    if raw_path != raw_path.strip() or not raw_path or "\\" in raw_path or "\x00" in raw_path:
        return f"invalid resource path: {raw_path!r}"
    if len(raw_path.encode("utf-8")) > 240:
        return f"resource path is too long: {raw_path!r}"
    parts = raw_path.split("/")
    if raw_path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        return f"unsafe resource path: {raw_path!r}"
    if any(part.startswith(".") for part in parts):
        return f"hidden resource paths are not allowed: {raw_path!r}"
    if any(any(ord(char) < 32 or ord(char) == 127 for char in part) for part in parts):
        return f"invalid resource path: {raw_path!r}"

    path = PurePosixPath(raw_path)
    name = path.name.casefold()
    if name == "skill.md":
        return "SKILL.md must be supplied via skill_md"
    if name == "install.sh":
        return "install.sh is not allowed in personal skills"
    if any(part.casefold() in _SECRET_DIRS for part in path.parts[:-1]):
        return f"secret resource paths are not allowed: {raw_path!r}"
    if name == ".env" or name.startswith(".env."):
        return f"secret resource paths are not allowed: {raw_path!r}"
    if (
        name in _SECRET_NAMES
        or name.endswith(_SECRET_SUFFIXES)
        or name.split(".", 1)[0] in _SECRET_STEMS
    ):
        return f"secret resource paths are not allowed: {raw_path!r}"
    return None


def _validation_error(args: SkillManageArgs) -> str | None:
    if len(args.name) > 64 or not _NAME_RE.fullmatch(args.name):
        return "name must be at most 64 characters of lowercase letters, digits, and single hyphens"
    if args.action == "export":
        return None
    if not args.skill_md:
        return "skill_md is required for create"
    encoded = args.skill_md.encode("utf-8")
    if len(encoded) > _MAX_SKILL_MD:
        return f"SKILL.md is too large (max {_MAX_SKILL_MD} bytes)"
    if len(args.files) > _MAX_FILES:
        return f"too many resource files (max {_MAX_FILES})"
    if len(encoded) + sum(len(f.content.encode("utf-8")) for f in args.files) > _MAX_TOTAL_TEXT:
        return f"skill package is too large (max {_MAX_TOTAL_TEXT} bytes)"

    paths: set[str] = set()
    for resource in args.files:
        if path_error := _resource_path_error(resource.path):
            return path_error
        if resource.path in paths:
            return f"duplicate resource path: {resource.path!r}"
        paths.add(resource.path)
    for path in paths:
        parts = PurePosixPath(path).parts
        for index in range(1, len(parts)):
            if PurePosixPath(*parts[:index]).as_posix() in paths:
                return f"conflicting resource path: {path!r}"

    metadata, body = parse_frontmatter(args.skill_md)
    if metadata.get("name") != args.name:
        return "SKILL.md frontmatter name must exactly match name"
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        return "SKILL.md frontmatter requires a non-empty description"
    if not body.strip():
        return "SKILL.md needs an instruction body"
    lowered = args.skill_md.lower()
    if any(marker in lowered for marker in ("todo: replace", "[todo]", "replace this text")):
        return "SKILL.md still contains scaffold placeholder text"
    return None


async def execute(args: SkillManageArgs, ctx: ToolContext) -> ToolResult:
    error = _validation_error(args)
    if error:
        return ToolResult(title="Skill package is not valid", output=error)
    if not ctx.sandbox:
        return ToolResult(
            title="Skill workspace unavailable",
            output="A sandbox is required to create or export a personal skill.",
        )

    if args.action == "create":
        created = False
        try:
            result = await ctx.sandbox.create_skill(
                name=args.name,
                skill_md=args.skill_md or "",
                files=[item.model_dump() for item in args.files],
            )
            created = True
            archive = await ctx.sandbox.download_skill_archive(args.name)
            from skill.user_library import upsert_personal_snapshot

            saved = await upsert_personal_snapshot(ctx.user_id, result, archive)
        except Exception as exc:
            rollback = ""
            if created:
                # The filesystem create is intentionally new-only.  If the
                # durable ownership snapshot fails afterwards, remove exactly
                # that just-created directory so a retry is not permanently
                # trapped behind a 409 conflict.
                try:
                    await ctx.sandbox.uninstall_skill(args.name)
                    rollback = " The incomplete filesystem copy was rolled back; creation can be retried."
                except Exception as rollback_exc:
                    rollback = (
                        " The filesystem copy may still exist because rollback failed: "
                        f"{str(rollback_exc)[:200]}"
                    )
            return ToolResult(
                title=f"Could not create {args.name}",
                output=f"{str(exc)[:500]}{rollback}",
            )

        return ToolResult(
            title=f"Created personal skill: {args.name}",
            output=(
                f"Personal skill '{args.name}' is installed and ready to load. "
                "It is listed in Skill Centre → Mine → Personal as Not uploaded. "
                "Publishing is a separate explicit action that makes this snapshot visible to every user."
            ),
            metadata={
                "skill_id": saved.get("id"),
                "name": args.name,
                "publication_status": saved.get("publication_status", "unpublished"),
                "files": result.get("files", []),
            },
        )

    try:
        info = await ctx.sandbox.get_skill(args.name)
        archive = await ctx.sandbox.download_skill_archive(args.name)
        from skill.user_library import upsert_personal_snapshot

        saved = await upsert_personal_snapshot(ctx.user_id, info, archive)
        exported = await ctx.sandbox.export_skill_archive(args.name)
    except Exception as exc:
        return ToolResult(title=f"Could not export {args.name}", output=str(exc)[:500])

    path = exported["path"]
    return ToolResult(
        title=f"Exported {args.name}.zip",
        output=(
            f"ZIP prepared at {path} ({exported.get('size', len(archive))} bytes). "
            "Call share_file with this exact path now so the user receives a downloadable attachment."
        ),
        metadata={
            "skill_id": saved.get("id"),
            "name": args.name,
            "path": path,
            "size": exported.get("size", len(archive)),
        },
    )


SKILL_MANAGE_DESCRIPTION = """\
Create or export one user-owned OpenBox skill package. Use create with a complete
validated SKILL.md and optional text resources; use export for a ZIP and pass its
path to share_file. Publishing remains a separate explicit action in Skill
Centre; the skill-creator skill provides authoring guidance only."""


skill_manage_tool = define_tool(
    "skill_manage",
    description=SKILL_MANAGE_DESCRIPTION,
    parameters=SkillManageArgs,
    execute=execute,
    parallel_safe=False,
)
