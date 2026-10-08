"""Shared SQL audience rules for session metadata and transcript read surfaces."""
from sqlalchemy import and_, exists, or_, select

from db.models.session import Session
from db.models.workspace import Workspace, WorkspaceMember


def active_membership(user_id: str, workspace_id):
    return exists(select(WorkspaceMember.user_id).join(
        Workspace, Workspace.id == WorkspaceMember.workspace_id,
    ).where(
        WorkspaceMember.user_id == user_id,
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.status == "active",
        Workspace.is_deleted.is_(False),
    ))


def session_audience(user_id: str, session=Session):
    """Include existence/title/counts, not only message bodies. Fail closed on kind."""
    return or_(
        session.user_id == user_id,
        and_(session.visibility == "workspace", session.kind != "assistant"),
    )


def readable_session(user_id: str, workspace_id: str, session=Session):
    return and_(
        session.workspace_id == workspace_id,
        session.is_deleted.is_(False),
        active_membership(user_id, workspace_id),
        session_audience(user_id, session),
    )


def asset_audience(user_id: str, asset):
    """A resource listing or signed URL must not expose private task outputs.

    Files remain available to their owner after a chat is removed. Unknown
    Session links never grant access to a workspace peer.
    """
    return and_(active_membership(user_id, asset.workspace_id), or_(
        asset.user_id == user_id,
        asset.session_id.is_(None),
        exists(select(Session.id).where(
            Session.id == asset.session_id, Session.workspace_id == asset.workspace_id,
            Session.visibility == "workspace", Session.kind != "assistant", Session.is_deleted.is_(False),
        )),
    ))


def usage_audience(user_id: str, usage):
    """Conversation-level billing metadata obeys the conversation's audience."""
    return and_(active_membership(user_id, usage.workspace_id), or_(
        usage.user_id == user_id,
        exists(select(Session.id).where(
            Session.id == usage.session_id, Session.workspace_id == usage.workspace_id,
            Session.visibility == "workspace", Session.kind != "assistant",
        )).correlate(usage),
    ))
