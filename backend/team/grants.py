"""Explicit owner edits to existing grants, including while a run is paused."""
from pydantic import Field, ValidationError

from agent_catalog.schemas import Contract, PaidToolGrant, PermissionGrant, TeamPolicy
from team.amendments import GRANT_FIELDS
from team.commands import run_status
from team.errors import TeamError
from team.journal import Actor, command
from team.state import check_revision


class GrantChange(Contract):
    expected_revision: int = Field(ge=1)
    paid_tools: dict[str, PaidToolGrant] | None = None
    permission_rules: list[PermissionGrant] | None = Field(default=None, max_length=128)
    max_coordinator_turns: int | None = Field(default=None, ge=1, le=500)
    max_wall_time_seconds: int | None = Field(default=None, ge=60, le=86400)


async def update(run_id: str, actor: Actor, key: str, change: GrantChange) -> dict:
    if actor.kind != "user":
        raise TeamError("AUTHORITY_REVOKED", "Only the owner may confirm a grant change.", status=403)
    changes = change.model_dump(mode="json", exclude_none=True, exclude={"expected_revision"})
    if not changes:
        raise TeamError("INVALID_GRANT", "Provide at least one grant or limit to change.", status=422)
    async def apply(writer):
        if writer.state["run"]["state"] not in {"running", "waiting", "paused"}:
            raise TeamError("TEAM_CLOSED", "Wait for a stable running or paused state before editing its grant.")
        check_revision(writer.state["run"], change.expected_revision)
        previous = writer.state["grant"]
        effective = {**writer.state["policy"], **{name: previous[name] for name in GRANT_FIELDS if name in previous}, **changes}
        try:
            policy = TeamPolicy.model_validate(effective)
        except ValidationError as exc:
            raise TeamError("INVALID_GRANT", str(exc), status=422) from exc
        from permission.permission import EDIT_TOOLS
        permissions = {"edit" if tool in EDIT_TOOLS else tool for tool in policy.delegable_tools}
        if any(rule.permission not in permissions for rule in policy.permission_rules):
            raise TeamError("INVALID_PERMISSION_GRANT", "An operation scope must belong to an already delegated tool.", status=422)
        if set(policy.paid_tools) - set(policy.delegable_tools):
            raise TeamError("INVALID_GRANT", "Paid permissions cannot add a tool outside the approved delegation.", status=422)
        limits = {name: changes[name] for name in ("max_coordinator_turns", "max_wall_time_seconds") if name in changes}
        grant = {**previous, **{name: value for name, value in changes.items() if name in GRANT_FIELDS},
            "id": run_id, "version": previous.get("version", 1) + 1}
        if limits:
            grant["limits"] = limits
        else:
            grant.pop("limits", None)
        writer.append("team.grant", "grant", grant)
        # Revision fences stale dialogs even when the run remains paused.
        run_status(writer, writer.state["run"]["state"])
        writer.append("team.notice", "notice", {"id": f"grant:{grant['version']}", "code": "OWNER_GRANT_UPDATED",
            "message": "The owner updated this run's permissions or execution limits.", "grant_version": grant["version"]})
        return {"id": run_id, "revision": writer.state["run"]["revision"], "grant_version": grant["version"],
            "seq": writer.state["seq"], "state": writer.state["run"]["state"], "root_session_id": writer.run.root_session_id}
    result = await command(run_id, actor, key, change.model_dump(mode="json"), apply)
    from bus import bus
    bus.publish("team.run.updated", {"userId": actor.owner_user_id, "sessionId": result["root_session_id"],
        "teamRunId": run_id, "seq": result["seq"], "state": result["state"]})
    return result
