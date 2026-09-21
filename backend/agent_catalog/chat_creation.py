"""Chat discovery, bounded atomic batches and reversible T0 publication."""
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agent_catalog import repository
from core.config import get_config
from db.base import get_db_session
from db.models.part import Part
from db.models.preference import UserPreference
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from db.models.team import AgentDefinition, AgentDefinitionVersion
from db.models.user import User
from team.errors import TeamError
from team.journal import digest, utcnow, write_transaction
from team.policy import tool_policy


async def require_skill_discovery(ctx):
    async with get_db_session() as db:
        rows = (await db.execute(select(Part).where(
            Part.session_id == ctx.session_id, Part.user_id == ctx.user_id,
            Part.type == "tool", Part.canonical_tool_id == "skill_search",
        ).order_by(Part.created_at.desc()).limit(50))).scalars().all()
    if not any(row.data.get("status") == "completed" and not row.data.get("error")
               and not (row.data.get("metadata") or {}).get("error") for row in rows):
        raise TeamError("AGENT_SKILL_DISCOVERY_REQUIRED",
            "Call skill_search in this conversation before proposing Agents, even if the result is empty. Choose relevant available Skills explicitly.", status=422)


async def check_limits(db, ctx, specs):
    questions = (await db.execute(select(QuestionCheckpoint).where(
        QuestionCheckpoint.session_id == ctx.session_id,
        QuestionCheckpoint.user_id == ctx.user_id))).scalars().all()
    proposals = [row for row in questions if row.continuation.get("kind") == "agent_proposal"]
    if any(row.status == "rejected" for row in proposals):
        raise TeamError("AGENT_PROPOSAL_DISMISSED", "The user dismissed Agent creation in this conversation. Do not propose it again.")
    rejected_names = set()
    for row in proposals:
        for index, answer in enumerate(row.answers or []):
            if answer == ["不要"] and index < len(row.questions):
                name = (row.questions[index].get("detail") or {}).get("spec", {}).get("name")
                if name:
                    rejected_names.add(name.casefold())
    if rejected_names.intersection(spec.name.casefold() for spec in specs):
        raise TeamError("AGENT_PROPOSAL_DISMISSED", "The user already declined this Agent name in this conversation.")
    rows = (await db.execute(select(AgentDefinition.provenance).where(
        AgentDefinition.owner_user_id == ctx.user_id, AgentDefinition.workspace_id == ctx.workspace_id,
        AgentDefinition.source == "ai"))).scalars().all()
    # Autoapproved creations have no question. Include them (and saved drafts
    # whose question has not committed yet), counting a batch as one proposal.
    keys = {row.part_id for row in proposals}
    keys.update(row.get("tool_call_id") for row in rows if row.get("session_id") == ctx.session_id)
    keys.discard(None)
    if ctx.part_id not in keys and len(keys) >= get_config().team_max_proposals_per_session:
        raise TeamError("AGENT_PROPOSAL_LIMIT", "This conversation reached its Agent proposal limit.")


async def create_batch(actor, ctx, specs, summaries):
    if not 1 <= len(specs) <= 4 or len(summaries) != len(specs):
        raise TeamError("INVALID_DEFINITION", "Create one to four validated Agent definitions at a time.", status=422)
    try:
        async with write_transaction() as db:
            await db.execute(select(User.id).where(User.id == actor.owner_user_id).with_for_update())
            session = (await db.execute(select(Session).where(Session.id == ctx.session_id).with_for_update())).scalar_one_or_none()
            if (session is None or session.user_id != actor.owner_user_id or session.workspace_id != actor.workspace_id
                    or session.parent_id or session.kind != "normal" or session.agent != "build"):
                raise TeamError("AUTHORITY_REVOKED", "Only the interactive build root can create reusable Agents.", status=403)
            await check_limits(db, ctx, specs)
            preference = (await db.execute(select(UserPreference).where(UserPreference.user_id == actor.owner_user_id).with_for_update())).scalar_one_or_none()
            auto = bool(preference and (preference.extra or {}).get("agent_autoapprove_t0") is True)
            auto = auto and all(not spec.mcp_refs and all(tool_policy(tool, get_config()).tier == "T0"
                for tool in spec.tool_allowlist) for spec in specs)
            previous_id = "agent_" + repository._receipt_key(actor, f"chat:{ctx.part_id}:0")[:56]
            previous = await db.get(AgentDefinition, previous_id)
            if previous is not None:
                # A retry returns the original outcome even if the preference
                # changed after the atomic batch committed.
                auto = previous.provenance.get("auto_approved") is True
            # Mixed batches always use one durable confirmation lifecycle.
            definitions = []
            for index, (spec, summary) in enumerate(zip(specs, summaries, strict=True)):
                definitions.append(await repository.create_locked(db, "agent", actor, f"chat:{ctx.part_id}:{index}", spec,
                    capability_summary=summary, source="ai", publish=auto,
                    provenance={"created_via": "chat", "session_id": ctx.session_id, "message_id": ctx.message_id,
                                "tool_call_id": ctx.part_id, "auto_approved": auto}))
            return definitions, auto
    except IntegrityError as exc:
        raise TeamError("DEFINITION_NAME_TAKEN", "An active definition already uses one of these names. No new definitions were saved.", status=409) from exc


async def undo_autoapproval(actor, definition_id, version_id, expected_revision, key):
    receipt_key = repository._receipt_key(actor, key)
    request_hash = digest({"action": "undo_autoapproval", "version_id": version_id, "expected_revision": expected_revision})
    async with write_transaction() as db:
        row = await repository.owned(db, "agent", definition_id, actor, lock=True)
        receipts = deepcopy(row.provenance.get("receipts", {}))
        if receipt_key in receipts:
            receipt = receipts[receipt_key]
            if receipt["digest"] != request_hash:
                raise TeamError("IDEMPOTENCY_CONFLICT", "This key was used for another change.")
            return deepcopy(receipt["result"])
        if (row.provenance.get("revision", 1) != expected_revision or row.current_version_id != version_id
                or row.draft_version_id or row.status != "active"):
            raise TeamError("STALE_REVISION", "This Agent changed since creation. Open its editor to review the current version.")
        if row.source != "ai" or row.provenance.get("auto_approved") is not True:
            raise TeamError("INVALID_DEFINITION_ACTION", "Only an automatically enabled Agent can be undone here.", status=422)
        version = await db.get(AgentDefinitionVersion, version_id)
        row.status, row.draft_version_id, row.current_version_id = "draft", version_id, None
        row.updated_at = utcnow()
        row.provenance = {**row.provenance, "revision": expected_revision + 1, "autoapproval_undone": True,
                          "published_version_ids": [], "current_version_number": None}
        result = repository._definition(row, version)
        receipts[receipt_key] = {"digest": request_hash, "result": result}
        row.provenance = {**row.provenance, "receipts": receipts}
        return result
