"""Safety net that assigns an owner's default workspace on legacy write paths.

Request-aware paths always set the selected workspace explicitly. Internal
callers written before workspace tenancy existed still get a valid scope
instead of attempting a NULL insert; this is especially important for cron
and recovery code that runs outside an HTTP request.
"""
from sqlalchemy import event, text

from db.models.cron import CronJob
from db.models.file_asset import FileAsset
from db.models.memory import UserMemory
from db.models.project import Project
from db.models.session import Session
from db.models.user_skill import UserSkill
from db.models.video_production import VideoProduction


_SCOPED_MODELS = (
    (Session, "user_id"),
    (Project, "user_id"),
    (FileAsset, "user_id"),
    (CronJob, "user_id"),
    (UserSkill, "owner_id"),
    (UserMemory, "user_id"),
    (VideoProduction, "user_id"),
)


def _assign_default_workspace(_mapper, connection, target) -> None:
    if getattr(target, "workspace_id", None):
        return
    session_id = getattr(target, "session_id", None)
    if session_id:
        session_workspace = connection.execute(
            text("SELECT workspace_id FROM sessions WHERE id = :session_id"),
            {"session_id": session_id},
        ).scalar_one_or_none()
        if session_workspace:
            target.workspace_id = session_workspace
            return
    owner_id = getattr(target, target.__workspace_owner_column__)
    workspace_id = connection.execute(
        text("SELECT default_workspace_id FROM users WHERE id = :user_id"),
        {"user_id": owner_id},
    ).scalar_one_or_none()
    # Unit tests historically construct owner rows directly and SQLite does
    # not enforce foreign keys by default. Production writers always have an
    # authenticated user with a provisioned default workspace.
    target.workspace_id = workspace_id or "ws_default"


for _model, _owner_column in _SCOPED_MODELS:
    _model.__workspace_owner_column__ = _owner_column
    event.listen(_model, "before_insert", _assign_default_workspace)
