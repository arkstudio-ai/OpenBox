"""Short database transactions for team commands and rebuildable state caches."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Awaitable, Callable, Literal
import weakref

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.identifier import ascending
from db.base import get_db_session, get_engine
from db.models.team import TeamEvent, TeamRun
from team.errors import TeamError
from team.state import TERMINAL, apply_event, empty_state, fold_events


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Actor:
    owner_user_id: str
    workspace_id: str
    kind: Literal["user", "member", "server"] = "user"
    member_id: str | None = None
    driver_run_id: str | None = None
    generation: int | None = None


_sqlite_guards: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
CACHE_FORMAT_VERSION = 2


@asynccontextmanager
async def write_transaction():
    """SQLite's database fence complements PostgreSQL's per-run row lock."""
    sqlite = get_engine().dialect.name == "sqlite"
    if sqlite:
        loop = asyncio.get_running_loop()
        guard = _sqlite_guards.setdefault(loop, asyncio.Lock())
        await guard.acquire()
    try:
        async with get_db_session() as db:
            if sqlite:
                await db.execute(text("BEGIN IMMEDIATE"))
            yield db
    finally:
        if sqlite:
            guard.release()


def event_dict(row: TeamEvent) -> dict:
    return {"id": row.id, "team_run_id": row.team_run_id, "sequence": row.sequence,
            "kind": row.kind, "entity_type": row.entity_type, "entity_id": row.entity_id,
            "actor_type": row.actor_type, "actor_member_id": row.actor_member_id,
            "payload": deepcopy(row.payload), "created_at": row.created_at.isoformat()}


async def owned_run(db: AsyncSession, run_id: str, actor: Actor, *, lock: bool = False) -> TeamRun:
    query = select(TeamRun).where(TeamRun.id == run_id, TeamRun.owner_user_id == actor.owner_user_id, TeamRun.workspace_id == actor.workspace_id)
    if lock:
        query = query.with_for_update()
    run = (await db.execute(query)).scalar_one_or_none()
    if run is None:
        raise TeamError("TEAM_NOT_FOUND", "Team run is not accessible.", status=404)
    return run


async def read_state(db: AsyncSession, run: TeamRun, *, rebuild: bool = False) -> dict:
    cached = run.state_cache
    if not rebuild and isinstance(cached, dict) and run.cache_seq == run.last_seq and cached.get("cache_format_version") == CACHE_FORMAT_VERSION:
        material = {key: value for key, value in cached.items() if key not in {"cache_digest", "cache_format_version"}}
        if (material.get("schema_version") == 1 and material.get("id") == run.id
                and material.get("seq") == run.last_seq and cached.get("cache_digest") == digest(material)):
            return deepcopy(material)
    rows = (await db.execute(select(TeamEvent).where(TeamEvent.team_run_id == run.id).order_by(TeamEvent.sequence))).scalars().all()
    state = fold_events(run.id, (event_dict(row) for row in rows))
    if state["seq"] != run.last_seq:
        raise TeamError("EVENT_SEQUENCE_GAP", "Team journal length differs from its committed watermark.")
    return state


def cache_state(run: TeamRun, state: dict) -> None:
    run.state_cache = {**deepcopy(state), "cache_digest": digest(state), "cache_format_version": CACHE_FORMAT_VERSION}
    run.cache_seq = state["seq"]
    run.last_seq = state["seq"]
    snapshot = state["run"]
    run.state = snapshot["state"]
    run.revision = snapshot["revision"]
    run.pause_reason = snapshot.get("pause_reason")
    run.final_summary = snapshot.get("final_summary")
    run.final_artifact_ids = snapshot.get("final_artifact_ids", [])
    run.grant_snapshot = deepcopy(state["grant"])
    run.policy_snapshot = deepcopy(state["policy"])
    boundaries = snapshot.get("workspace_snapshots", {})
    run.start_snapshot = boundaries.get("start", {}).get("hash")
    run.end_snapshot = boundaries.get("end", {}).get("hash")
    run.session_active = None if run.state in TERMINAL else 1
    run.project_active = run.session_active
    run.updated_at = utcnow()
    if run.state in TERMINAL:
        run.ended_at = datetime.fromisoformat(snapshot["ended_at"]) if snapshot.get("ended_at") else run.updated_at
    members = list(state["members"].values())
    tasks = list(state["tasks"].values())
    run.summary = {
        **(run.summary or {}),
        "member_count": len(members),
        "temporary_member_count": sum(m["source"] == "coordinator" for m in members),
        "task_count": len(tasks),
        "completed_task_count": sum(t["state"] == "succeeded" for t in tasks),
        "artifact_count": len(state["artifacts"]),
        "needs_attention": run.state == "paused" or any(t["state"] in {"blocked", "failed", "outcome_unknown"} for t in tasks),
    }


async def validate_actor(db: AsyncSession, run: TeamRun, state: dict, actor: Actor) -> None:
    if actor.kind in {"user", "server"}:
        return
    if actor.kind != "member" or actor.member_id not in state["members"]:
        raise TeamError("AUTHORITY_REVOKED", "Actor has no membership in this run.", status=403)
    if state["members"][actor.member_id]["membership_state"] != "active":
        raise TeamError("AUTHORITY_REVOKED", "Member is no longer active.", status=403)
    from db.models.agent_driver import AgentDriverState
    from db.models.session import Session
    from sqlalchemy import func
    # Match the durable transport fence, under a row lock, for every command.
    query = select(AgentDriverState).join(Session, Session.id == AgentDriverState.session_id).where(
        AgentDriverState.session_id == actor.member_id,
        AgentDriverState.user_id == actor.owner_user_id,
        AgentDriverState.run_id == actor.driver_run_id,
        AgentDriverState.generation == actor.generation,
        AgentDriverState.phase != "idle",
        AgentDriverState.lease_expires_at > func.current_timestamp(),
        AgentDriverState.abort_requested_at.is_(None),
        Session.user_id == run.owner_user_id,
        Session.workspace_id == run.workspace_id,
        Session.project_id == run.project_id,
        Session.is_deleted.is_(False),
    ).with_for_update()
    lease = (await db.execute(query)).scalar_one_or_none()
    if lease is None:
        raise TeamError("STALE_GENERATION", "This member's Driver lease is no longer valid.", status=409)


class Writer:
    """A command may append events and SQL writes, but must never perform I/O."""
    def __init__(self, db: AsyncSession, run: TeamRun, actor: Actor, key: str, request_digest: str, state: dict):
        self.db, self.run, self.actor = db, run, actor
        self.key, self.request_digest, self.state = key, request_digest, state
        self.events: list[TeamEvent] = []

    def append(self, kind: str, entity_type: str, data: dict) -> dict:
        event_key = self.key if not self.events else digest(f"{self.key}:{len(self.events)}")
        row = TeamEvent(id=ascending("tevent"), team_run_id=self.run.id,
            sequence=self.state["seq"] + 1, event_key=event_key,
            request_digest=self.request_digest, kind=kind, actor_type=self.actor.kind,
            actor_member_id=self.actor.member_id, entity_type=entity_type, entity_id=data["id"],
            payload={"schema_version": 1, "data": deepcopy(data)}, created_at=utcnow())
        self.state = apply_event(self.state, event_dict(row))
        self.events.append(row)
        return data

    def finish(self, result: dict) -> dict:
        if not self.events:
            self.append("team.notice", "notice", {"id": ascending("tnotice"), "code": "COMMAND_UNCHANGED"})
        self.events[0].payload = {**self.events[0].payload, "command_result": deepcopy(result)}
        # SQL reads in mutate() may autoflush. Buffer complete events so every
        # journal row is inserted once, without a later payload UPDATE.
        self.db.add_all(self.events)
        cache_state(self.run, self.state)
        return result


async def command(run_id: str, actor: Actor, key: str, request: dict,
                  mutate: Callable[[Writer], Awaitable[dict]]) -> dict:
    """Serialize, authorize, deduplicate and commit a complete team command."""
    if not isinstance(key, str) or not key or len(key) > 256:
        raise TeamError("IDEMPOTENCY_KEY_REQUIRED", "Use a stable, nonempty idempotency key (up to 256 characters).", status=422)
    event_key = digest({"actor": actor.kind, "member": actor.member_id, "key": key})
    request_digest = digest(request)
    async with write_transaction() as db:
        run = await owned_run(db, run_id, actor, lock=True)
        state = await read_state(db, run)
        await validate_actor(db, run, state, actor)
        prior = (await db.execute(select(TeamEvent).where(TeamEvent.team_run_id == run_id, TeamEvent.event_key == event_key))).scalar_one_or_none()
        if prior is not None:
            if prior.request_digest != request_digest:
                raise TeamError("IDEMPOTENCY_CONFLICT", "This key already refers to a different command.")
            return deepcopy(prior.payload["command_result"])
        writer = Writer(db, run, actor, event_key, request_digest, state)
        result = writer.finish(await mutate(writer))
        await db.flush()
    # Notification and runtime scheduling are post-commit work at the service layer.
    return result


async def snapshot(run_id: str, actor: Actor, *, rebuild: bool = False) -> dict:
    async with get_db_session() as db:
        run = await owned_run(db, run_id, actor)
        return await read_state(db, run, rebuild=rebuild)
