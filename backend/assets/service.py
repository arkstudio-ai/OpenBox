"""SQL asset selection; listing never signs URLs or moves file bytes."""
from sqlalchemy import and_, or_, select

from db.models.file_asset import FileAsset
from session.policy import asset_audience


def owned_ready_query(user_id, workspace_id=None):
    return select(FileAsset).where(FileAsset.user_id == user_id,
        *([FileAsset.workspace_id == workspace_id] if workspace_id else []),
        FileAsset.status == "ready", FileAsset.is_deleted.is_(False))


def resource_query(user_id, workspace_id, *, owned_only=False):
    from db.models.session import Session
    from db.models.video_job import VideoJob
    from light.remake_case import job_versions

    # Older directors published constituent takes and stills with transient=false.
    # Keep their working files directly accessible to the editor and its saved drafts.
    remake_take = select(VideoJob.id).where(
        VideoJob.output_asset_id == FileAsset.id, VideoJob.session_id == FileAsset.session_id,
        VideoJob.user_id == FileAsset.user_id, VideoJob.kind == "segment",
        VideoJob.idempotency_key.startswith("light:"),
        or_(*(VideoJob.idempotency_key.contains(f":{version}:") for version in job_versions())),
    ).exists()
    remake_still = and_(
        FileAsset.name.regexp_match(r"^wan-story-[0-9a-f]{28}-commercial-"),
        select(Session.id).where(Session.id == FileAsset.session_id, Session.light_entry == "video").exists(),
    )
    query = select(FileAsset).where(FileAsset.workspace_id == workspace_id,
        FileAsset.is_deleted.is_(False), FileAsset.status == "ready",
        FileAsset.transient.is_(False), asset_audience(user_id, FileAsset),
        or_(FileAsset.source != "agent", and_(~remake_take, ~remake_still)))
    return query.where(FileAsset.user_id == user_id) if owned_only else query
