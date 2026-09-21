"""Load frozen team authority at the legacy subagent binding seam."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from agent.subagent_authority import SubagentAuthorityError, parse_subagent_authority
from agent.subagent_composition import validate_composition_availability, SubagentCompositionError
from core.config import get_config
from db.base import get_db_session
from db.models.team import TeamEvent, TeamRun
from team.journal import read_state
from team.policy import COORDINATOR_READ_TOOLS, COORDINATOR_TOOLS, MEMBER_TOOLS, tool_policy


@dataclass(frozen=True)
class RuntimeBinding:
    run_id: str | None
    member_id: str
    owner_user_id: str
    workspace_id: str
    project_id: str
    role: str
    spec: dict
    admission: dict


_current: ContextVar[RuntimeBinding | None] = ContextVar("team_runtime_binding", default=None)


def current_binding() -> RuntimeBinding | None:
    return _current.get()


async def load_binding(session: Any):
    """Return a team/trial authority or None; team members never fall through."""
    _current.set(None)
    from agent_catalog.trials import PREFIX, authority_for_version
    agent_name = str(getattr(session, "agent", "") or "")
    if agent_name.startswith(PREFIX):
        if getattr(session, "parent_id", None) or getattr(session, "kind", "normal") != "agent_trial":
            raise SubagentAuthorityError("A definition binding requires an interactive trial session")
        from team.journal import Actor
        from team.errors import TeamError
        try:
            authority, spec, summary = await authority_for_version(agent_name[len(PREFIX):], Actor(session.user_id, session.workspace_id))
        except (TeamError, SubagentCompositionError) as exc:
            raise SubagentAuthorityError(str(exc)) from exc
        _current.set(RuntimeBinding(None, session.id, session.user_id, session.workspace_id, session.project_id, "trial", spec, {"capability_summary": summary}))
        return authority
    is_member = getattr(session, "kind", "normal") == "team_member"
    parent = getattr(session, "parent_id", None)
    if parent and not is_member:
        return None
    async with get_db_session() as db:
        if is_member:
            rows = (await db.execute(select(TeamEvent, TeamRun).join(TeamRun, TeamRun.id == TeamEvent.team_run_id).where(
                TeamEvent.entity_id == session.id, TeamEvent.kind == "team.member.admitted",
                TeamRun.owner_user_id == session.user_id,
            ))).all()
            if len(rows) != 1:
                raise SubagentAuthorityError("Team member has no unique durable admission")
            admission_row, run = rows[0]
            if parent != run.root_session_id:
                raise SubagentAuthorityError("Team member lineage mismatch")
            from db.models.subagent import SubagentDescriptor
            if (await db.execute(select(SubagentDescriptor.id).where(SubagentDescriptor.child_session_id == session.id))).first():
                raise SubagentAuthorityError("Session has both legacy and team authority")
        else:
            run = (await db.execute(select(TeamRun).where(TeamRun.root_session_id == session.id,
                TeamRun.owner_user_id == session.user_id, TeamRun.session_active == 1))).scalar_one_or_none()
            if run is None:
                return None
            admission_row = (await db.execute(select(TeamEvent).where(TeamEvent.team_run_id == run.id,
                TeamEvent.entity_id == session.id, TeamEvent.kind == "team.member.admitted"))).scalar_one_or_none()
            if admission_row is None:
                raise SubagentAuthorityError("Active team has no coordinator admission")
        if run.project_id != session.project_id or run.workspace_id != session.workspace_id:
            raise SubagentAuthorityError("Team binding crosses a tenant or project")
        if run.state not in {"provisioning", "running", "waiting"}:
            raise SubagentAuthorityError("Team is paused or closed to model execution")
        state = await read_state(db, run)
        member = state["members"].get(session.id)
        if member is None or member["membership_state"] != "active":
            raise SubagentAuthorityError("Team member is not active")
        admission = admission_row.payload["data"]
        authority = parse_subagent_authority(admission["authority"])
        try:
            validate_composition_availability(authority.composition, get_config())
        except SubagentCompositionError as exc:
            raise SubagentAuthorityError(str(exc)) from exc
        _current.set(RuntimeBinding(run.id, session.id, session.user_id, run.workspace_id,
            run.project_id, member["role"], admission["spec"], admission))
        return authority


async def current_grant() -> dict:
    binding = current_binding()
    if binding is None or binding.run_id is None:
        return {}
    from db.models.project import Project
    from db.models.user import User
    from db.models.workspace import Workspace, WorkspaceMember
    async with get_db_session() as db:
        run = (await db.execute(select(TeamRun).join(User, User.id == TeamRun.owner_user_id)
            .join(Workspace, Workspace.id == TeamRun.workspace_id)
            .join(WorkspaceMember, (WorkspaceMember.workspace_id == TeamRun.workspace_id)
                & (WorkspaceMember.user_id == TeamRun.owner_user_id))
            .join(Project, Project.id == TeamRun.project_id).where(TeamRun.id == binding.run_id,
            TeamRun.owner_user_id == binding.owner_user_id,
            TeamRun.workspace_id == binding.workspace_id, TeamRun.project_id == binding.project_id,
            User.is_active.is_(True), Workspace.is_deleted.is_(False),
            WorkspaceMember.status == "active", Project.is_deleted.is_(False)))).scalar_one_or_none()
        if run is None or run.state not in {"running", "waiting", "provisioning"}:
            raise SubagentAuthorityError("Team no longer permits execution")
        state = await read_state(db, run)
        member = state["members"].get(binding.member_id)
        if member is None or member["membership_state"] != "active":
            raise SubagentAuthorityError("Team membership is no longer active")
        return run.grant_snapshot


async def restrict_current_tools(tools: dict, *, session=None) -> dict:
    """Intersect the frozen tools with live deployment and user-run grants."""
    binding = current_binding()
    if binding is None:
        if session is not None and (getattr(session, "parent_id", None) or getattr(session, "kind", "normal") != "normal" or getattr(session, "agent", "build") == "plan"):
            return {key: value for key, value in tools.items() if key not in COORDINATOR_TOOLS | {"agent_manage"}}
        if session is not None and getattr(session, "agent", None) == "team":
            proposal_tools = {"team_propose", "agent_catalog_search", "agent_catalog_get"}
            return {key: value for key, value in tools.items() if key not in COORDINATOR_TOOLS - proposal_tools}
        return tools
    grant = await current_grant()
    if binding.role == "coordinator":
        return {key: value for key, value in tools.items() if key in COORDINATOR_TOOLS | COORDINATOR_READ_TOOLS}
    from team.mcp import META_TOOLS, meta_tools_allowed
    mcp_allowed = await meta_tools_allowed()
    if binding.role == "trial":
        permitted = set(binding.spec["tool_allowlist"]) | (META_TOOLS if mcp_allowed else set())
        categories = set(binding.spec.get("execution_policy", {}).get("tool_categories", []))
        result = {}
        from team.errors import TeamError
        for key, value in tools.items():
            if key not in permitted:
                continue
            if key in META_TOOLS and mcp_allowed:
                result[key] = value
                continue
            try:
                policy = tool_policy(key, get_config())
            except TeamError:
                continue
            if not categories or policy.tier in categories:
                result[key] = value
        return result
    config = get_config()
    permitted = set(grant.get("delegable_tools", []))
    categories = set(binding.spec.get("execution_policy", {}).get("tool_categories", []))
    result = {}
    from team.errors import TeamError
    for key, value in tools.items():
        if key in META_TOOLS and mcp_allowed:
            result[key] = value
        elif key in MEMBER_TOOLS:
            result[key] = value
        elif key in permitted:
            try:
                policy = tool_policy(key, config)
            except TeamError:
                continue
            if categories and policy.tier not in categories:
                continue
            frozen_group = binding.admission.get("capability_summary", {}).get("exclusive_group")
            if policy.exclusive_group and policy.exclusive_group != frozen_group:
                # A hot plugin replacement may require desktop exclusivity.
                # An already admitted member cannot acquire that lane silently.
                continue
            if policy.tier != "T2" or key in grant.get("paid_tools", {}):
                result[key] = value
    return result


async def assert_tool_current(tool_id: str, ctx) -> None:
    """Recheck live grants after model output, immediately before tool effects."""
    binding = current_binding()
    if binding is None:
        return
    from team.execution import assert_execution_time
    await assert_execution_time(ctx)
    from team.errors import TeamError
    if (ctx.user_id != binding.owner_user_id or ctx.workspace_id != binding.workspace_id
            or ctx.project_id != binding.project_id or ctx.session_id != binding.member_id):
        raise TeamError("AUTHORITY_REVOKED", "The tool context no longer matches this team member.", status=403)
    if tool_id.startswith("mcp:v2:"):
        from team.mcp import preapproved as mcp_preapproved
        if await mcp_preapproved(tool_id, ["*"], {}):
            return
    elif tool_id in await restrict_current_tools({tool_id: True}):
        if binding.role != "member" or tool_id in MEMBER_TOOLS | set(binding.spec.get("tool_allowlist", [])):
            return
        from team.mcp import META_TOOLS
        if tool_id in META_TOOLS:
            return
    raise TeamError("TOOL_NOT_ALLOWED", "This tool is no longer in the member's approved capabilities.", status=403)


async def preapproved(permission: str, patterns: list[str], input_data: dict | None = None) -> bool | None:
    """None means interactive; False means a member must report a blocker."""
    binding = current_binding()
    if binding is None or binding.role != "member":
        return None
    from permission.permission import Rule, evaluate
    from team.mcp import preapproved as mcp_preapproved
    mcp_approval = await mcp_preapproved(permission, patterns, input_data or {})
    if mcp_approval is not None:
        return mcp_approval
    grant = await current_grant()
    rules = [Rule.model_validate(value) for value in grant.get("permission_rules", [])]
    return all(evaluate(permission, pattern, rules).action == "allow" for pattern in patterns)
