"""Bounded, non-executing validation of an operator's untrusted ZIP upload."""

import hashlib
import re
import stat
import zipfile
import yaml
import zlib
from io import BytesIO
from pathlib import PurePosixPath

from core.markdown import parse_frontmatter

MAX_ZIP_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 2000
MAX_MANIFEST_BYTES = 64 * 1024
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def packaging_metadata(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        "__MACOSX" in path.parts
        or path.name == ".DS_Store"
        or path.name.startswith("._")
    )


def skill_slug(value: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or not SLUG.fullmatch(value):
        raise ValueError("Skill name must be a lowercase slug of at most 64 characters")
    return value


def manifest_metadata(content: str) -> dict:
    if len(content.encode("utf-8")) > MAX_MANIFEST_BYTES or not content.startswith(
        "---"
    ):
        raise ValueError("A SKILL.md with YAML frontmatter is required (maximum 64 KB)")
    try:
        metadata, _ = parse_frontmatter(content)
    except (yaml.YAMLError, TypeError, ValueError) as exc:
        raise ValueError("Invalid SKILL.md YAML frontmatter") from exc
    if not isinstance(metadata, dict):
        raise ValueError("Invalid SKILL.md metadata")
    name = skill_slug(metadata.get("name"))
    description = metadata.get("description")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > 4000
    ):
        raise ValueError("SKILL.md description is required (maximum 4000 characters)")
    icon = metadata.get("icon", "")
    dependencies = metadata.get("requires_mcp") or []
    if (
        not isinstance(dependencies, list)
        or len(dependencies) > 50
        or any(
            not isinstance(dep, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", dep)
            for dep in dependencies
        )
    ):
        raise ValueError("requires_mcp must be a list of at most 50 MCP identifiers")
    return {
        "name": name,
        "title": name,
        "description": description.strip(),
        "icon": icon if isinstance(icon, str) and len(icon) <= 16 else "",
        "requires_mcp": list(dict.fromkeys(dependencies)),
    }


def validate_zip(data: bytes) -> dict:
    if not data or len(data) > MAX_ZIP_BYTES:
        raise ValueError("ZIP must be non-empty and at most 32 MB")
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_MEMBERS:
                raise ValueError("ZIP has too many files (maximum 2000)")
            total = 0
            names: set[str] = set()
            manifests = []
            for member in members:
                path = PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.filename
                    or ":" in member.filename
                    or "\x00" in member.filename
                    or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
                    or member.flag_bits & 1
                ):
                    raise ValueError(
                        "ZIP contains unsafe paths, links or encrypted members"
                    )
                key = str(path).casefold()
                if key in names:
                    raise ValueError("ZIP contains duplicate paths")
                names.add(key)
                total += member.file_size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError("Expanded ZIP exceeds 64 MB")
                if (
                    not member.is_dir()
                    and path.name == "SKILL.md"
                    and not packaging_metadata(member.filename)
                ):
                    manifests.append(member)
            # A single root/wrapped skill per ZIP; multiple ZIPs form a batch.
            if len(manifests) != 1:
                raise ValueError("Each ZIP must contain exactly one SKILL.md")
            manifest = manifests[0]
            root = PurePosixPath(manifest.filename).parent
            if len(root.parts) > 1 or any(
                not PurePosixPath(m.filename).is_relative_to(root)
                for m in members
                if not packaging_metadata(m.filename)
            ):
                raise ValueError(
                    "SKILL.md must be at ZIP root or inside one wrapper directory"
                )
            if manifest.file_size > MAX_MANIFEST_BYTES:
                raise ValueError("SKILL.md exceeds 64 KB")
            metadata = manifest_metadata(archive.read(manifest).decode("utf-8"))
            # Read every member to validate CRC/decompression, one at a time.
            for member in members:
                if not member.is_dir():
                    with archive.open(member) as stream:
                        remaining = member.file_size + 1
                        while remaining > 0:
                            chunk = stream.read(min(remaining, 65536))
                            if not chunk:
                                break
                            remaining -= len(chunk)
                        if remaining == 0:
                            raise ValueError("ZIP member exceeds declared size")
    except (
        zipfile.BadZipFile,
        UnicodeError,
        RuntimeError,
        NotImplementedError,
        zlib.error,
        EOFError,
    ) as exc:
        raise ValueError(
            "ZIP is corrupt, encrypted or uses an unsupported format"
        ) from exc
    return {
        **metadata,
        "archive_size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def prepare_zip(data: bytes) -> tuple[bytes, dict]:
    """Strip Finder metadata, which otherwise breaks one-wrapper VM installs."""
    metadata = validate_zip(data)
    with zipfile.ZipFile(BytesIO(data)) as source:
        if not any(packaging_metadata(member.filename) for member in source.infolist()):
            return data, metadata
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
            for member in source.infolist():
                if not packaging_metadata(member.filename):
                    target.writestr(member, source.read(member))
    cleaned = buffer.getvalue()
    return cleaned, validate_zip(cleaned)


def zip_manifest(name: str, content: str) -> bytes:
    metadata = manifest_metadata(content)
    if metadata["name"] != skill_slug(name):
        raise ValueError("Skill name must match SKILL.md frontmatter")
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", content)
    return buffer.getvalue()
