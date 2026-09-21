"""Apply the exact Agent version shown in a saved question, in its transaction."""
from agent_catalog.repository import owned
from db.models.team import AgentDefinitionVersion
from team.journal import Actor, utcnow


async def apply_answer(db, session, checkpoint, metadata):
    saved = checkpoint.continuation
    actor = Actor(checkpoint.user_id, session.workspace_id)
    if saved["workspace_id"] != actor.workspace_id:
        raise ValueError("The Agent proposal belongs to another workspace")
    definitions = saved.get("definitions") or [saved]
    rows = {item["definition_id"]: await owned(db, "agent", item["definition_id"], actor, lock=True)
            for item in sorted(definitions, key=lambda item: item["definition_id"])}
    if checkpoint.status == "rejected":
        return {"title": "Agent proposal saved as draft", "output": "The user dismissed this proposal. Leave the drafts untouched and do not propose Agent creation again in this conversation.",
                "metadata": {**metadata, "definition_ids": list(rows), "rejected": True}}, []
    candidates = []
    answers = checkpoint.answers or []
    # Validate the whole saved batch before changing any publication pointer.
    for index, item in enumerate(definitions):
        row = rows[item["definition_id"]]
        version = await db.get(AgentDefinitionVersion, item["version_id"])
        if (row.status == "archived" or row.provenance.get("revision", 1) != item["revision"]
                or row.draft_version_id != item["version_id"] or version is None
                or version.definition_id != row.id or version.content_digest != item["content_digest"]):
            raise ValueError("The Agent draft changed after this card was shown. Open its editor or request a fresh confirmation.")
        answer = answers[index] if index < len(answers) else []
        decision = answer[0] if len(answer) == 1 else ""
        if decision == "启用":
            validate_publication(version)
        candidates.append((row, version, answer, decision))
    outputs, changes = [], []
    for row, version, answer, decision in candidates:
        if decision == "启用":
            published = list(dict.fromkeys(value for value in [*row.provenance.get("published_version_ids", []), row.current_version_id, version.id] if value))
            row.current_version_id, row.draft_version_id, row.status = version.id, None, "active"
            row.provenance = {**row.provenance, "published_version_ids": published, "current_version_number": version.version}
            output = "The user enabled this exact Agent version. It is now available in the team catalog."
        elif decision == "不要":
            row.status, row.active_name = "archived", None
            output = "The user declined this Agent. It is archived; do not propose it again."
        else:
            output = "The Agent remains a draft and cannot join teams. User response: " + str(answer)
        row.updated_at = utcnow()
        row.provenance = {**row.provenance, "revision": row.provenance.get("revision", 1) + 1}
        outputs.append(f"{row.name}: {output}")
        changes.append({"definition_id": row.id, "decision": decision})
    return {"title": "Agent proposal answered", "output": "\n".join(outputs),
            "metadata": {**metadata, "agent_decisions": changes, **(changes[0] if len(changes) == 1 else {})}}, []


def validate_publication(version):
    from agent.subagent_authority import parse_subagent_authority
    from agent.subagent_composition import validate_composition_availability
    from core.config import get_config
    from team.policy import tool_policy
    material = version.capability_summary.get("_compiled_trial")
    if not material:
        raise ValueError("This Agent version has no validated configuration")
    authority = parse_subagent_authority(material["authority"])
    validate_composition_availability(authority.composition, get_config())
    for tool in version.spec_json["tool_allowlist"]:
        tool_policy(tool, get_config())
