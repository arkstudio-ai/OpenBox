"""Run admission and user control using the existing question/Inbox lifecycle."""
from __future__ import annotations

from sqlalchemy import select

from agent_catalog.compiler import CompiledAgent
from agent_catalog.schemas import MemberSpec, TeamPolicy
from core.identifier import ascending
from db.base import get_db_session
from db.models.session import Session
from db.models.team import TeamRun
from team.commands import member_status, require_coordinator, require_open, run_status
from team.errors import TeamError
from team.journal import Actor, Writer, command, digest, owned_run, utcnow
from team.state import empty_state


async def admit_member(writer: Writer, member: MemberSpec, compiled: CompiledAgent, *, source: str, version_id: str | None = None) -> dict:
    from team.runtime import runtime
    require_coordinator(writer)
    state = writer.state
    if member.alias in {m["alias"] for m in state["members"].values()}:
        raise TeamError("TEAM_MEMBER_ALIAS_TAKEN", "This alias was already used in the team.")
    row = await runtime.create_member(writer.db, writer.run, compiled, member.alias)
    data = {"id": row.id, "alias": member.alias, "role": "member", "source": source,
        "definition_id": member.agent_ref, "version_id": version_id,
        "name": compiled.spec.name, "description": compiled.spec.description, "display": compiled.spec.display.model_dump(),
        "model": compiled.summary["model"], "tool_ids": compiled.summary["tool_ids"],
        "skill_refs": compiled.summary["skills"], "exclusive_group": compiled.summary["exclusive_group"],
        "responsibility": member.responsibility, **compiled.snapshot()}
    writer.append("team.member.admitted", "member", data)
    member_status(writer, row.id, membership_state="active")
    return {"member": writer.state["members"][row.id]}


async def start_confirmed_locked(db, *, root: Session, question_id: str, title: str, goal: str,
                                 policy: TeamPolicy, grant: dict, coordinator: CompiledAgent,
                                 members: list[tuple[MemberSpec, CompiledAgent, str | None]],
                                 template_id: str | None = None, template_version_id: str | None = None,
                                 configuration: dict | None = None) -> dict:
    """Only a confirmed question continuation invokes this, in its transaction.

The caller already holds the root/execution lock and marks the checkpoint
applied in this same transaction. No endpoint can directly start a team.
"""
    from core.config import get_config
    if not get_config().team_admission_enabled:
        raise TeamError("TEAM_ADMISSION_DISABLED", "New team runs are disabled.", status=403)
    from team.runtime import runtime
    capabilities = runtime.capabilities()
    if not all((capabilities.model_selection, capabilities.steer, capabilities.resume, capabilities.cancel)):
        raise TeamError("CAPABILITY_UNSUPPORTED", "This runtime cannot execute the required team lifecycle.", status=422)
    if root.kind == "team_member" or root.parent_id:
        raise TeamError("AUTHORITY_REVOKED", "Only a root session may start a team.", status=403)
    if not title.strip() or not goal.strip() or len(goal) > 32000 or len(title) > 255:
        raise TeamError("INVALID_TEAM", "A bounded title and goal are required.", status=422)
    from agent_catalog import repository
    from team.policy import tool_policy
    owner = Actor(root.user_id, root.workspace_id)
    if template_id:
        template = await repository.owned(db, "team", template_id, owner)
        if template.status != "active":
            raise TeamError("AGENT_NOT_ACCESSIBLE", "The selected template was archived before confirmation.")
    for member, compiled, _ in members:
        if member.agent_ref and not member.agent_ref.startswith("builtin:"):
            definition = await repository.owned(db, "agent", member.agent_ref, owner)
            if definition.status != "active":
                raise TeamError("AGENT_NOT_ACCESSIBLE", "A selected Agent was archived before confirmation.")
        for tool_id in compiled.spec.tool_allowlist:
            tool_policy(tool_id, get_config())
        from team.mcp import validate_refs
        validate_refs(compiled.spec, get_config(), grant)
    prior = (await db.execute(select(TeamRun).where(TeamRun.root_session_id == root.id, TeamRun.session_active == 1))).scalar_one_or_none()
    if prior:
        raise TeamError("TEAM_ALREADY_ACTIVE", "This conversation already has an active team.", current={"id": prior.id})
    # Serialize different roots starting in the same project. The unique
    # active-project constraint is the final backstop on every database.
    from db.models.project import Project
    project = (await db.execute(select(Project).where(Project.id == root.project_id,
        Project.user_id == root.user_id, Project.workspace_id == root.workspace_id, Project.is_deleted.is_(False)).with_for_update())).scalar_one_or_none()
    if project is None:
        raise TeamError("TEAM_NOT_FOUND", "Project is not accessible.", status=404)
    occupied = (await db.execute(select(TeamRun.id).where(TeamRun.project_id == root.project_id, TeamRun.project_active == 1))).scalar_one_or_none()
    if occupied:
        raise TeamError("TEAM_ALREADY_ACTIVE", "This project already has an active team.", current={"id": occupied})
    run_id, now = ascending("team"), utcnow()
    policy_json = policy.model_dump(mode="json")
    run = TeamRun(id=run_id, root_session_id=root.id, owner_user_id=root.user_id,
        workspace_id=root.workspace_id, project_id=root.project_id, title=title, goal=goal,
        template_id=template_id, template_version_id=template_version_id,
        summary={"confirmation_id": question_id, "team_configuration": configuration}, policy_snapshot=policy_json, grant_snapshot=grant,
        state="provisioning", revision=1, session_active=1, project_active=1,
        created_at=now, updated_at=now)
    db.add(run)
    await db.flush()
    writer = Writer(db, run, Actor(root.user_id, root.workspace_id, "server"), digest(f"confirm:{question_id}"), digest({"question_id": question_id}), empty_state(run_id))
    writer.append("team.run.created", "run", {"id": run_id, "root_session_id": root.id,
        "owner_user_id": root.user_id, "workspace_id": root.workspace_id, "project_id": root.project_id,
        "title": title, "goal": goal, "created_at": now.isoformat(), "state": "provisioning", "revision": 1,
        "policy_snapshot": policy_json, "grant_snapshot": grant,
        "workspace_snapshots": {"start": {"status": "pending"}, "end": {"status": "pending"}}})
    writer.append("team.member.admitted", "member", {"id": root.id, "alias": "coordinator", "role": "coordinator",
        "source": "builtin", "name": coordinator.spec.name, "description": coordinator.spec.description, "display": coordinator.spec.display.model_dump(),
        "model": coordinator.summary["model"], "tool_ids": coordinator.summary["tool_ids"], "skill_refs": coordinator.summary["skills"], **coordinator.snapshot()})
    member_status(writer, root.id, membership_state="active")
    for member, compiled, version_id in members:
        await admit_member(writer, member, compiled, source="template" if member.agent_ref else "coordinator", version_id=version_id)
    root.agent = "team"
    root.model = coordinator.summary["model"]
    run_status(writer, "running")
    result = writer.finish({"id": run_id, "members": list(writer.state["members"].values()), "seq": writer.state["seq"]})
    await db.flush()
    return result


async def control(run_id: str, actor: Actor, key: str, action: str, expected_revision: int, reason: str = "") -> dict:
    if actor.kind != "user":
        raise TeamError("AUTHORITY_REVOKED", "Run controls require the owner.", status=403)
    if action not in {"pause", "resume", "cancel"}:
        raise TeamError("INVALID_TEAM_CONTROL", "Unsupported team control.", status=422)
    async def mutate(writer):
        from team.state import check_revision
        check_revision(writer.state["run"], expected_revision)
        target = {"pause": "pausing", "resume": "running", "cancel": "canceling"}[action]
        run_status(writer, target, pause_reason=reason or ("user" if action == "pause" else None))
        if action == "resume":
            from team.runtime import runtime
            await runtime.enqueue(writer.db, writer.run, writer.run.root_session_id, {"kind": "team_control", "after_seq": writer.state["seq"]},
                f"team:resume:{writer.run.id}:{writer.state['run']['revision']}", "用户已恢复团队。请查看最新任务状态，继续可执行任务并处理被中断的任务。")
        return {"id": run_id, "state": target, "revision": writer.state["run"]["revision"]}
    result = await command(run_id, actor, key, {"action": action, "expected_revision": expected_revision, "reason": reason}, mutate)
    if action == "resume":
        await resume_saved_answer(run_id, actor)
    from team.scheduler import schedule
    schedule(run_id, actor)
    return result


async def resume_saved_answer(run_id: str, actor: Actor) -> None:
    """Repair an interrupted question continuation without changing its answer.

    Keep the session/question lock after, not inside, the team command lock:
    question application takes those locks in the opposite order.
    """
    from question import runtime
    from db.models.question import QuestionCheckpoint
    async with get_db_session() as db:
        run = await owned_run(db, run_id, actor)
        root_id = run.root_session_id
    async with runtime.transaction(root_id, actor.owner_user_id, fence=False) as (db, session, execution):
        rows = (await db.scalars(select(QuestionCheckpoint).where(
            QuestionCheckpoint.session_id == root_id,
            QuestionCheckpoint.user_id == actor.owner_user_id,
            QuestionCheckpoint.generation == execution.generation,
            QuestionCheckpoint.status.in_(["answered", "rejected"]),
            QuestionCheckpoint.applied.is_(False)))).all()
        if any(row.continuation.get("kind") == "team_lineup" and row.continuation.get("run_id") == run_id for row in rows):
            execution.resume_pending = True
            execution.resume_error = None
            execution.next_attempt_at = None
            if session.status == "error":
                session.status = "idle"


async def cancel_waiting_questions(run_id: str, actor: Actor) -> None:
    """Cancel the closing team's suspended turn before releasing its root.

    Question transactions lock the session first. Keep this outside the team
    journal transaction and recheck the active run while holding that lock so
    a later ordinary conversation's questions cannot be canceled.
    """
    from db.models.question import QuestionCheckpoint
    from question import runtime
    async with get_db_session() as db:
        run = await owned_run(db, run_id, actor)
        root_id = run.root_session_id
    rows = []
    async with runtime.transaction(root_id, actor.owner_user_id, fence=False) as (db, session, execution):
        run = await db.get(TeamRun, run_id)
        if run.session_active != 1 or run.state != "canceling":
            return
        pending = await db.scalar(select(QuestionCheckpoint.id).where(
            QuestionCheckpoint.session_id == root_id, QuestionCheckpoint.user_id == actor.owner_user_id,
            QuestionCheckpoint.generation == execution.generation, QuestionCheckpoint.applied.is_(False),
            QuestionCheckpoint.status.in_(["pending", "answered", "rejected"])).limit(1))
        if pending and not execution.run_id:
            rows = await runtime.invalidate_locked(db, execution, "cancelled")
            session.status = "idle"
    if rows:
        runtime.publish_invalidated(rows)
        await runtime.publish_status(root_id, actor.owner_user_id, "idle")


async def finish(writer: Writer, summary: str, artifact_ids: list[str], *, status: str = "completed", reason: str | None = None,
                 response_message_id: str | None = None, response_tool_part_id: str | None = None) -> dict:
    require_coordinator(writer)
    require_open(writer)
    if set(artifact_ids) - set(writer.state["artifacts"]):
        raise TeamError("INVALID_ARTIFACT", "Final artifacts must belong to this run.")
    if status not in {"completed", "failed"} or (status == "failed" and not (reason or "").strip()):
        raise TeamError("INVALID_TEAM_RESULT", "A failed outcome requires a concrete reason.", status=422)
    blockers = [task for task in writer.state["tasks"].values()
        if task["state"] in {"blocked", "failed", "outcome_unknown"}]
    if blockers and status == "completed":
        raise TeamError("DELIVERABLES_INCOMPLETE",
            "Resolve the team's blocked or failed work before finishing. A blocker report does not fulfill the original goal; ask the user if it cannot be resolved.",
            current=[{"id": task["id"], "state": task["state"], "revision": task["revision"]} for task in blockers])
    publication = ({"final_response": {"message_id": response_message_id, "tool_part_id": response_tool_part_id}}
        if response_message_id and response_tool_part_id else {})
    run_status(writer, "completing", final_summary=summary, final_artifact_ids=artifact_ids,
        final_status=status, failure_reason=reason if status == "failed" else None, **publication)
    return {"state": "completing", "final_status": status, "seq": writer.state["seq"], "turn_yield": True, "summary": summary}


async def active_for_session(session_id: str, owner_user_id: str) -> tuple[str, str] | None:
    async with get_db_session() as db:
        row = (await db.execute(select(TeamRun.id, TeamRun.workspace_id).join(Session,
            (Session.id == TeamRun.root_session_id) | (Session.parent_id == TeamRun.root_session_id)).where(
                Session.id == session_id, TeamRun.owner_user_id == owner_user_id, TeamRun.session_active == 1))).first()
        return (row.id, row.workspace_id) if row else None
