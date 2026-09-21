"""User-confirmed additions and grant revisions for an existing team.

The question checkpoint owns the proposed configuration. Only its authenticated
answer continuation can atomically revise the grant and admit these members.
"""
from __future__ import annotations

from dataclasses import replace
import json

from sqlalchemy import select

from agent_catalog.catalog import prepare_lineup, restore_lineup
from agent_catalog.schemas import CoordinatorSpec, TeamPolicy, TeamSpec
from db.models.team import TeamEvent
from team.errors import TeamError
from team.journal import Actor, Writer, digest, owned_run, read_state, snapshot

GRANT_FIELDS = frozenset({"member_selection", "member_creation", "allowed_agent_ids", "allowed_models",
    "delegable_tools", "allowed_skills", "mcp_refs", "paid_tools", "permission_rules"})


async def prepare(run_id: str, actor: Actor, proposed: TeamSpec, *, scope=None):
    state = await snapshot(run_id, actor)
    if actor.kind != "member" or actor.member_id != state["run"]["root_session_id"]:
        raise TeamError("AUTHORITY_REVOKED", "Only the active coordinator may propose an amendment.", status=403)
    if state["run"]["state"] not in {"running", "waiting"}:
        raise TeamError("TEAM_PAUSED", "Resume the team before proposing a roster change.")
    additions = [member for member in proposed.preset_members if member.enabled]
    aliases = {member["alias"] for member in state["members"].values()}
    if any(member.alias in aliases for member in additions):
        raise TeamError("TEAM_MEMBER_ALIAS_TAKEN", "An amendment lists only new members. Existing, retired and failed aliases cannot be reused.")
    if len(state["members"]) + len(additions) > state["policy"]["max_members"]:
        raise TeamError("TEAM_MEMBER_LIMIT", "The original run's member limit includes retired and failed members.")
    changes = proposed.policy.model_dump(mode="json", exclude_unset=True)
    if any(key not in GRANT_FIELDS and value != state["policy"].get(key) for key, value in changes.items()):
        raise TeamError("INVALID_AMENDMENT", "An amendment may revise grants but cannot replace this run's scheduling limits.", status=422)
    policy = {**state["policy"], **{key: state["grant"][key] for key in GRANT_FIELDS if key in state["grant"]}, **changes}
    if policy["allowed_agent_ids"]:
        policy["allowed_agent_ids"] = list(dict.fromkeys([*policy["allowed_agent_ids"], *(m.agent_ref for m in additions if m.agent_ref)]))
    effective = TeamPolicy.model_validate(policy)
    coordinator = CoordinatorSpec(model=state["members"][actor.member_id]["model"])
    # Explicitly approved inline additions are named exceptions. They do not
    # turn on unprompted future member creation in a fixed roster.
    compile_policy = effective.model_copy(update={"member_creation": "run_scoped", "member_selection": "coordinator_select"})
    spec = proposed.model_copy(update={"policy": compile_policy, "preset_members": additions, "coordinator": coordinator})
    compiled = await prepare_lineup(spec, actor, scope=scope)
    grant = {**compiled.grant, "version": state["grant"].get("version", 1) + 1,
        "member_selection": effective.member_selection, "member_creation": effective.member_creation}
    # The restored TeamSpec still describes the approved exact additions;
    # its policy may be fixed with no additions for a grant-only amendment.
    approved_spec = spec.model_copy(update={"policy": effective})
    prepared = replace(compiled, spec=approved_spec, grant=grant)
    return prepared, {"run_id": run_id, "expected_grant_version": state["grant"].get("version", 1),
        "roster_digest": digest(sorted(state["members"]))}


async def apply_answer(db, session, row, metadata):
    continuation = row.continuation
    if row.status == "rejected" or row.answers != [["应用调整"]]:
        return {"title": "Team amendment not applied", "output": "The existing roster and grants are unchanged. User response: " + json.dumps(row.answers, ensure_ascii=False),
            "metadata": {**metadata, "team_lineup": True, "rejected": True}}, []
    actor = Actor(row.user_id, session.workspace_id, "server")
    run = await owned_run(db, continuation["run_id"], actor, lock=True)
    if run.root_session_id != session.id or session.parent_id or session.kind != "normal":
        raise TeamError("AUTHORITY_REVOKED", "This confirmation belongs to another team root.", status=403)
    state = await read_state(db, run)
    key = digest(f"amend-confirm:{row.id}")
    prior = (await db.execute(select(TeamEvent).where(TeamEvent.team_run_id == run.id, TeamEvent.event_key == key))).scalar_one_or_none()
    if prior is not None:
        result = prior.payload["command_result"]
    else:
        if run.state not in {"running", "waiting"}:
            code = "TEAM_PAUSED" if run.state in {"pausing", "paused"} else "TEAM_CLOSED"
            raise TeamError(code, "The saved amendment can be applied only while this team is running; resume a paused run first.")
        if (state["grant"].get("version", 1) != continuation["expected_grant_version"]
                or digest(sorted(state["members"])) != continuation["roster_digest"]):
            raise TeamError("STALE_REVISION", "The roster or grant changed after this proposal. Request fresh confirmation.")
        prepared = restore_lineup(continuation["lineup"], amendment=True)
        from agent_catalog import repository
        from core.config import get_config
        from team.policy import tool_policy
        from team.service import admit_member
        config = get_config()
        if not config.team_admission_enabled:
            raise TeamError("TEAM_ADMISSION_DISABLED", "New team admissions are disabled. Request confirmation after the deployment reopens admissions.")
        for member, compiled, _ in prepared.members:
            if member.agent_ref and not member.agent_ref.startswith("builtin:"):
                definition = await repository.owned(db, "agent", member.agent_ref, actor)
                if definition.status != "active":
                    raise TeamError("AGENT_NOT_ACCESSIBLE", "A proposed Agent was archived before confirmation.")
            for tool_id in compiled.spec.tool_allowlist:
                tool_policy(tool_id, config)
            from team.mcp import validate_refs
            validate_refs(compiled.spec, config, prepared.grant)
        writer = Writer(db, run, actor, key, digest(continuation), state)
        writer.append("team.grant", "grant", {**prepared.grant, "id": run.id})
        added = [await admit_member(writer, member, compiled, source="confirmed", version_id=version)
            for member, compiled, version in prepared.members]
        result = writer.finish({"id": run.id, "grant_version": writer.state["grant"]["version"],
            "members": [item["member"] for item in added], "seq": writer.state["seq"]})
        await db.flush()
    events = [{"type": "team.run.updated", "data": {"userId": row.user_id, "sessionId": session.id,
        "teamRunId": run.id, "seq": result["seq"], "state": run.state}}]
    return {"title": "Team amendment confirmed", "output": "The user approved this exact amendment: " + json.dumps(result, ensure_ascii=False),
        "metadata": {**metadata, "team_lineup": True, "team_run_id": run.id,
            "team_member_changes": [{"kind": "joined", "id": member["id"], "name": member.get("name") or member["alias"]} for member in result["members"]]}}, events
