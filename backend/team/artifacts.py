"""Verify and freeze deliverables outside the short team journal transaction."""
from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
from pathlib import Path

from db.base import get_db_session
from db.models.file_asset import FileAsset
from team.errors import TeamError
from team.journal import digest, owned_run, utcnow

MAX_BYTES = 512 * 1024 * 1024


def snapshot_key(workspace_id: str, run_id: str, content_digest: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", content_digest):
        raise TeamError("INVALID_ARTIFACT", "Artifact digest must be a verified SHA-256.")
    return f"team-artifacts/{workspace_id}/{run_id}/{content_digest}"


def require_owned(asset, run):
    if (asset is None or asset.user_id != run.owner_user_id or asset.workspace_id != run.workspace_id
            or asset.project_id != run.project_id or asset.is_deleted or asset.status != "ready"):
        raise TeamError("INVALID_ARTIFACT", "Artifact asset is not available in this project.")


async def prepare(run_id, actor, references: list[dict]) -> list[dict]:
    """Hash actual object bytes; a model-supplied digest is only a comparison.

    Published snapshot keys have no public PUT endpoint. They are written once
    with forbid_overwrite, so a still-valid source upload URL cannot mutate an
    accepted deliverable. Existing FileAsset access/deletion rules still apply.
    """
    if not references:
        return []
    async with get_db_session() as db:
        run = await owned_run(db, run_id, actor)
        sources = []
        for ref in references:
            source = await db.get(FileAsset, ref["file_asset_id"])
            require_owned(source, run)
            if not 0 <= source.size <= MAX_BYTES:
                raise TeamError("INVALID_ARTIFACT", "Artifact exceeds the 512 MiB verification limit.")
            sources.append(source)
    from core.oss import get_oss
    try:
        oss = get_oss()
        verified = []
        for ref, source in zip(references, sources, strict=True):
            with tempfile.TemporaryDirectory(prefix="openbox-team-artifact-") as directory:
                path = Path(directory) / "content"
                hasher, size = hashlib.sha256(), 0
                with path.open("wb") as target:
                    async for chunk in oss.get_object_chunks(source.oss_key):
                        size += len(chunk)
                        if size > MAX_BYTES or size > source.size:
                            raise TeamError("INVALID_ARTIFACT", "Artifact bytes changed after asset registration.")
                        hasher.update(chunk)
                        await asyncio.to_thread(target.write, chunk)
                content_digest = hasher.hexdigest()
                if size != source.size or (ref.get("content_digest") and ref["content_digest"] != content_digest):
                    raise TeamError("INVALID_ARTIFACT", "Artifact size or SHA-256 does not match its actual bytes.")
                key = snapshot_key(run.workspace_id, run.id, content_digest)
                await oss.put_object_file(key, path, content_type=source.mime, forbid_overwrite=True)
            identifier = "asset_team_" + digest([run.id, source.id, content_digest])[:48]
            async with get_db_session() as db:
                current = await db.get(FileAsset, source.id)
                require_owned(current, run)
                if (current.oss_key, current.size) != (source.oss_key, source.size):
                    raise TeamError("INVALID_ARTIFACT", "Artifact source changed while verifying its bytes.")
                if db.bind.dialect.name == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert
                else:
                    from sqlalchemy.dialects.sqlite import insert
                await db.execute(insert(FileAsset).values(id=identifier, user_id=run.owner_user_id,
                    workspace_id=run.workspace_id, project_id=run.project_id, session_id=source.session_id,
                    name=source.name, mime=source.mime, size=size, oss_key=key, status="ready",
                    source="agent", transient=True, is_deleted=False, created_at=utcnow()
                ).on_conflict_do_nothing(index_elements=["id"]))
                require_owned(await db.get(FileAsset, identifier), run)
            verified.append({**ref, "source_file_asset_id": source.id,
                "file_asset_id": identifier, "content_digest": content_digest})
        return verified
    except TeamError:
        raise
    except Exception as exc:
        raise TeamError("ARTIFACT_VERIFICATION_FAILED", "Could not verify and freeze the artifact bytes; retry submission when storage is available.") from exc
