"""Resolve the explicit composer selection before accepting a model's lineup."""
from __future__ import annotations

from sqlalchemy import select

from agent_catalog import repository
from agent_catalog.schemas import MemberSpec, TeamRequest, TeamSpec
from db.base import get_db_session
from db.models.message import Message
from db.models.part import Part
from team.journal import Actor


async def selected_lineup(session_id: str, actor: Actor, proposed: TeamSpec) -> tuple[TeamSpec, dict]:
    async with get_db_session() as db:
        rows = (await db.execute(select(Part.data).join(Message, Message.id == Part.message_id).where(
            Part.session_id == session_id, Part.user_id == actor.owner_user_id, Part.type == "text", Message.role == "user")
            .order_by(Message.created_at.desc(), Message.id.desc()).limit(200))).scalars().all()
    request = next((TeamRequest.model_validate(row["team_request"]) for row in rows if row.get("team_request")), None)
    if request is None:
        return proposed, {}
    references = {}
    spec = proposed.model_copy(deep=True)
    if request.template_id and request.template_id != "auto":
        template = await repository.get("team", request.template_id, actor, active_only=True)
        spec = TeamSpec.model_validate(template["version"]["spec"])
        references = {"template_id": template["id"], "template_version_id": template["version"]["id"]}
    if request.allow_supplement is not None:
        spec.policy.member_selection = "coordinator_select" if request.allow_supplement else "explicit_only"
    existing = {member.agent_ref for member in spec.preset_members}
    for index, identifier in enumerate(request.requested_agent_ids):
        if identifier not in existing:
            alias = f"selected_{index + 1}"
            while alias in {member.alias for member in spec.preset_members}:
                alias += "_"
            spec.preset_members.append(MemberSpec(alias=alias, agent_ref=identifier))
            existing.add(identifier)
    return TeamSpec.model_validate(spec.model_dump(mode="json")), references
