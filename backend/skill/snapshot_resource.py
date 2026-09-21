"""Bounded first-read snapshots for host and sandbox Skill resources."""
from __future__ import annotations

import base64
import json
import inspect
import shlex

from team.errors import TeamError


def confined_read(root_path: str, relative: str, limit: int) -> bytes:
    """Pin each directory with openat; a concurrent symlink swap cannot escape."""
    import os
    import stat
    from pathlib import Path
    root = Path(root_path).resolve(strict=True)
    target = (root / relative).resolve(strict=True)
    parts = target.relative_to(root).parts
    if not parts:
        raise ValueError("Skill resource is not a file")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open(root.anchor, directory_flags)
    try:
        for part in [*root.parts[1:], *parts[:-1]]:
            child = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("Skill resource is not a regular file")
            return handle.read(limit + 1)
    finally:
        os.close(directory)


async def read_resource_bytes(material: dict, relative: str, ctx, limit: int) -> bytes:
    try:
        if material.get("path"):
            raw = confined_read(material["path"], relative, limit)
        elif material.get("base_dir") and ctx.sandbox:
            # Resolve symlinks in the execution plane before opening a listed
            # resource. A shell 'head' on the joined path would follow links
            # outside the Skill and lose binary bytes through UTF-8 decoding.
            script = "\n".join([
                "import base64,json,sys",
                inspect.getsource(confined_read),
                "raw=confined_read(sys.argv[1],sys.argv[2],int(sys.argv[3]))",
                "print(json.dumps({'base64':base64.b64encode(raw).decode('ascii')}))",
            ])
            command = "python3 -c " + shlex.quote(script) + " " + " ".join(shlex.quote(arg) for arg in (material["base_dir"], relative, str(limit)))
            result = await ctx.sandbox.execute(command, timeout=10)
            if result.exit_code != 0:
                raise TeamError("SKILL_RESOURCE_NOT_ACCESSIBLE", "The resource is unavailable or leaves its Skill directory before its first read.", status=422)
            raw = base64.b64decode(json.loads(result.stdout)["base64"], validate=True)
        else:
            raise TeamError("SKILL_RESOURCE_NOT_ACCESSIBLE", "The resource provider is unavailable before its first read.", status=422)
    except (OSError, ValueError, KeyError) as exc:
        if isinstance(exc, TeamError):
            raise
        raise TeamError("SKILL_RESOURCE_NOT_ACCESSIBLE", "The resource could not be read safely within its Skill directory before its first snapshot.", status=422) from exc
    if len(raw) > limit:
        raise TeamError("SKILL_SNAPSHOT_TOO_LARGE", "The resource exceeds the 1 MiB snapshot limit.", status=422)
    return raw


def encode_resource(raw: bytes) -> dict:
    try:
        body = raw.decode("utf-8")
        if "\x00" not in body:
            return {"body": body, "encoding": "utf-8", "size": len(raw)}
    except UnicodeDecodeError:
        pass
    return {"body": base64.b64encode(raw).decode("ascii"), "encoding": "base64", "size": len(raw)}


def render_resource(material: dict) -> str:
    if material.get("encoding", "utf-8") == "utf-8":
        return material["body"]
    # Binary resources remain byte-exact and are explicitly identified, rather
    # than being silently decoded with replacement characters.
    return json.dumps(material, ensure_ascii=False)
