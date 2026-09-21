"""The team runtime port; all model execution remains in Driver and Inbox."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_catalog.compiler import CompiledAgent
from core.identifier import ascending
from db.models.session import Session
from db.models.team import TeamRun
from team.journal import utcnow


@dataclass(frozen=True)
class RuntimeCapabilities:
    model_selection: bool = True
    steer: bool = True
    resume: bool = True
    cancel: bool = True
    artifact_types: tuple[str, ...] = ("file_asset",)


@dataclass(frozen=True)
class ExecutionSnapshot:
    session_id: str
    run_id: str | None
    generation: int
    phase: str
    started_at: datetime | None
    lease_expires_at: datetime | None

    @property
    def live(self) -> bool:
        expiry = self.lease_expires_at
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return bool(self.run_id and self.phase != "idle" and expiry and expiry > utcnow())


@dataclass(frozen=True)
class ReconciliationSnapshot:
    execution: ExecutionSnapshot | None
    accepted_input: bool
    unresolved_effects: tuple[dict, ...]


class RuntimeAdapter(Protocol):
    def capabilities(self) -> RuntimeCapabilities: ...
    async def create_member(self, db: AsyncSession, run: TeamRun, compiled: CompiledAgent, alias: str) -> Session: ...
    async def enqueue(self, db: AsyncSession, run: TeamRun, member_id: str, source: dict, key: str, prompt: str) -> str: ...
    async def observe(self, db: AsyncSession, member_ids, user_id: str) -> dict[str, ExecutionSnapshot]: ...
    async def reconcile(self, db: AsyncSession, member_id: str, user_id: str, driver_run_id: str | None) -> ReconciliationSnapshot: ...
    async def wake(self, member_id: str, user_id: str) -> str | None: ...
    async def interrupt(self, member_id: str, user_id: str, *, expected_run_id: str, expected_generation: int) -> bool: ...


class OpenBoxRuntimeAdapter:
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities()

    async def create_member(self, db, run, compiled, alias):
        now = utcnow()
        row = Session(id=ascending("session"), user_id=run.owner_user_id,
            workspace_id=run.workspace_id, project_id=run.project_id,
            parent_id=run.root_session_id, kind="team_member", agent="team_member",
            model=compiled.authority.composition.model, variant=compiled.spec.reasoning,
            title=f"{compiled.spec.name} · {alias}", status="idle", created_at=now, updated_at=now)
        db.add(row)
        await db.flush()
        return row

    async def enqueue(self, db, run, member_id, source, key, prompt):
        from agent.inbox import accept_team_input_locked
        from team.errors import TeamError
        row = await db.get(Session, member_id, with_for_update=True)
        if row is None:
            raise TeamError("TEAM_MEMBER_NOT_FOUND", "Member session is missing.")
        result = await accept_team_input_locked(db, session_row=row, team_run=run,
            source={"team_run_id": run.id, **source}, client_id=key, prompt=prompt)
        return result.id

    async def wake(self, member_id, user_id):
        from agent.inbox import wake_inbox_session
        return await wake_inbox_session(member_id, user_id)

    async def observe(self, db, member_ids, user_id):
        from db.models.agent_driver import AgentDriverState
        rows = (await db.scalars(select(AgentDriverState).join(Session, Session.id == AgentDriverState.session_id)
            .where(AgentDriverState.session_id.in_(member_ids), AgentDriverState.user_id == user_id,
                Session.user_id == user_id, Session.is_deleted.is_(False)))).all()
        return {row.session_id: ExecutionSnapshot(row.session_id, row.run_id, row.generation,
            row.phase, row.started_at, row.lease_expires_at) for row in rows}

    async def reconcile(self, db, member_id, user_id, driver_run_id):
        """Read durable execution evidence; never replay an external request.

        The existing recovery service repairs Driver/Inbox tails before the
        team phase. A non-idle expired generation remains fenced for that
        service; the team may classify it only after it becomes idle.
        """
        from db.models.agent_inbox import AgentInboxItem
        from db.models.external_effect import ExternalEffect
        execution = (await self.observe(db, [member_id], user_id)).get(member_id)
        accepted = bool(await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.session_id == member_id,
            AgentInboxItem.user_id == user_id, AgentInboxItem.state == "accepted").limit(1)))
        effects = (await db.scalars(select(ExternalEffect).where(ExternalEffect.session_id == member_id,
            ExternalEffect.tenant_id == user_id, ExternalEffect.run_id == driver_run_id,
            ExternalEffect.state.in_(["prepared", "submitting", "accepted", "outcome_unknown", "manual_review"])))).all()
        return ReconciliationSnapshot(execution, accepted, tuple({"id": row.id, "state": row.state} for row in effects))

    async def interrupt(self, member_id, user_id, *, expected_run_id, expected_generation):
        from agent.driver import request_abort
        return await request_abort(member_id, user_id,
            expected_run_id=expected_run_id, expected_generation=expected_generation)


runtime: RuntimeAdapter = OpenBoxRuntimeAdapter()
