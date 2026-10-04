"""Current private-session audience for trace replicas, exports and hints.

The trace worker uses the business authority callback, never its own cached
metadata as a viewing grant. Ordinary platform-admin diagnostics keep their
existing audience. Source-content provenance is a separate check.
"""
from functools import wraps
from inspect import signature

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

from trajectory.auth import UNAVAILABLE, get_backend


MAX_AUDIENCE_SOURCES = 2000


class SessionAudienceSource(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    workspace_id: str | None = Field(default=None, max_length=64)


class SessionAudienceTarget(SessionAudienceSource):
    sources: list[SessionAudienceSource] = Field(default_factory=list, max_length=MAX_AUDIENCE_SOURCES)


class SessionAudienceQuery(BaseModel):
    targets: list[SessionAudienceTarget] = Field(max_length=200)

    @model_validator(mode="after")
    def unique_sessions(self):
        if len({target.session_id for target in self.targets}) != len(self.targets):
            raise ValueError("A session must have one original owner/workspace binding")
        if sum(len(target.sources) for target in self.targets) > MAX_AUDIENCE_SOURCES:
            raise ValueError("Recorded session source budget exceeded")
        return self


def session_target(session, trajectory=None):
    if trajectory is not None:
        return {"session_id": trajectory.session_id, "user_id": trajectory.user_id,
                "workspace_id": trajectory.workspace_id}
    return {"session_id": session.id, "user_id": session.user_id, "workspace_id": session.workspace_id}


async def visible_sessions(viewer_id, targets):
    if not targets:
        return set()
    if len(targets) > 200:
        raise ValueError("Trajectory audience batch exceeds 200 sessions")
    requested = {target["session_id"] for target in targets}
    try:
        if len(requested) != len(targets):
            raise ValueError("Duplicate trajectory session bindings")
        targets = await recorded_sources(targets)
        if not targets:
            return set()
        requested = {target["session_id"] for target in targets}
        value = await get_backend().session_audience(viewer_id, targets)
        allowed = value.get("allowed") if isinstance(value, dict) else None
        if (not isinstance(allowed, list) or value.get("version") != 2 or value.get("user_id") != viewer_id
                or any(not isinstance(identity, str) or identity not in requested for identity in allowed)
                or len(set(allowed)) != len(allowed)):
            raise ValueError("Invalid trajectory audience response")
    except Exception as exc:
        raise HTTPException(503, detail=UNAVAILABLE) from exc
    return set(allowed)


async def recorded_sources(targets):
    """Worker-owned bindings, refreshed on every read/hint/list check.

    A client cannot omit a recorded child or supply a new owner for old bytes.
    Legacy recordings stay unavailable until the bounded worker backfill has
    accounted for every original event, including events now in archive blobs.
    """
    from sqlalchemy import select
    from trajectory.store.database import trace_read_session
    from trajectory.store.models import SessionTrajectory, TrajectorySessionSource
    ids = [target["session_id"] for target in targets]
    async with trace_read_session() as db:
        recordings = {row.session_id: row for row in (await db.scalars(select(SessionTrajectory).where(
            SessionTrajectory.session_id.in_(ids)))).all()}
        rows = (await db.scalars(select(TrajectorySessionSource).where(
            TrajectorySessionSource.trajectory_id.in_([row.id for row in recordings.values()]))
            .limit(MAX_AUDIENCE_SOURCES + 1))).all() if recordings else []
    if len(rows) > MAX_AUDIENCE_SOURCES:
        raise ValueError("Recorded session source budget exceeded")
    grouped = {}
    for row in rows:
        grouped.setdefault(row.trajectory_id, []).append({"session_id": row.session_id,
            "user_id": row.user_id, "workspace_id": row.workspace_id})
    verified = []
    for target in targets:
        row = recordings.get(target["session_id"])
        sources = []
        if row is not None:
            if (row.deleted_at is not None
                    or row.audience_seq < row.committed_seq or row.user_id != target["user_id"]
                    or row.workspace_id != target["workspace_id"]):
                continue
            sources = grouped.get(row.id, [])
            if row.committed_seq and not sources:
                continue
        verified.append({**target, "sources": sources})
    return verified


async def require_sessions(viewer_id, targets):
    if await visible_sessions(viewer_id, targets) != {target["session_id"] for target in targets}:
        raise LookupError("Session not found")


def session_read(function):
    """Check before I/O and again before returning stored content or a stream.

    Bind to the replica's original owner/workspace as well as its session ID.
    A failed final check closes an already-spooled download without sending it.
    """
    parameters = signature(function)

    @wraps(function)
    async def guarded(*args, **kwargs):
        from trajectory import repository
        from trajectory.store.database import trace_read_session
        bound = parameters.bind(*args, **kwargs).arguments
        viewer, session_id = bound["admin"]["user_id"], bound["session_id"]
        async with trace_read_session() as db:
            session, trajectory = await repository.get_trajectory(db, session_id, optional=True)
            target = session_target(session, trajectory)
        await require_sessions(viewer, [target])
        response = await function(*args, **kwargs)
        try:
            await require_sessions(viewer, [target])
        except BaseException:
            if close := getattr(response, "close", None):
                close()
            raise
        return response
    return guarded
