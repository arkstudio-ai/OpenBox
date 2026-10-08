"""SQL asset selection; listing never signs URLs or moves file bytes."""
from sqlalchemy import select

from db.models.file_asset import FileAsset
from session.policy import asset_audience


def owned_ready_query(user_id, workspace_id=None):
    return select(FileAsset).where(FileAsset.user_id == user_id,
        *([FileAsset.workspace_id == workspace_id] if workspace_id else []),
        FileAsset.status == "ready", FileAsset.is_deleted.is_(False))


def resource_query(user_id, workspace_id, *, owned_only=False):
    query = select(FileAsset).where(FileAsset.workspace_id == workspace_id,
        FileAsset.is_deleted.is_(False), FileAsset.status == "ready",
        FileAsset.transient.is_(False), asset_audience(user_id, FileAsset))
    return query.where(FileAsset.user_id == user_id) if owned_only else query
