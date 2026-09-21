"""Immutable Skill knowledge storage, independent of optional trace storage.

Production uses the existing asset OSS client. Local development can select an
explicit directory; multi-host deployments must use OSS or a shared filesystem.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path, PurePosixPath


def checked_key(key: str) -> str:
    if (not isinstance(key, str) or len(key) > 1024 or not key.startswith("team-skills/")
            or "\\" in key or "\x00" in key or any(part in {"", ".", ".."} for part in key.split("/"))):
        raise ValueError("Invalid Skill snapshot key")
    return str(PurePosixPath(key))


class LocalSkillStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def _path(self, key: str) -> Path:
        path = self.root / checked_key(key)
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Skill snapshot key escapes its storage directory")
        return path

    async def put(self, key: str, data: bytes, *, content_type: str, if_absent: bool = True) -> None:
        await asyncio.to_thread(self._write, self._path(key), bytes(data), if_absent)

    @staticmethod
    def _write(path: Path, data: bytes, if_absent: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if if_absent:
                # Atomic first-writer-wins, including the lazy first-read
                # references whose competing payloads can contain new content.
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    return
            else:
                os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)


class OssSkillStore:
    @staticmethod
    async def _call(method, key, **kwargs):
        from core.oss import get_oss
        try:
            return await getattr(get_oss(), method)(checked_key(key), **kwargs)
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise RuntimeError("Skill snapshot object storage is unavailable") from exc

    async def put(self, key: str, data: bytes, *, content_type: str, if_absent: bool = True) -> None:
        await self._call("put_object", key, data=bytes(data), content_type=content_type, forbid_overwrite=if_absent)

    async def get(self, key: str) -> bytes:
        return await self._call("get_object", key)

    async def exists(self, key: str) -> bool:
        return await self._call("head_object_info", key) is not None


def get_blob_store() -> LocalSkillStore | OssSkillStore:
    from core.config import get_config
    provider = os.environ.get("TEAM_SKILL_BLOB_PROVIDER") or ("oss" if get_config().oss_bucket else "local")
    if provider == "oss":
        return OssSkillStore()
    if provider == "local":
        return LocalSkillStore(os.environ.get("TEAM_SKILL_BLOB_LOCAL_PATH") or
            Path(__file__).resolve().parents[1] / ".openbox" / "team-skill-blobs")
    raise RuntimeError("TEAM_SKILL_BLOB_PROVIDER must be local or oss")
