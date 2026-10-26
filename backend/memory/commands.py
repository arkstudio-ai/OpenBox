"""Source-bound conversational writes. Model prose is a proposal, never user confirmation."""
import asyncio
from hashlib import sha256

from sqlalchemy import select

from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.memory import UserMemory
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from memory import service
from memory.grounding import GroundingVerifier
from memory.policy import MemoryAccessDenied, resolve_access_scope
from memory.preferences import REVISION_KEY, apply_profile
from memory.redaction import json_hash, mask_sensitive
from memory.session_policy import extraction_project_id, memory_extraction_eligible
from memory.versions import previous_receipt, record_receipt


async def human_sources(db, *, user_id, workspace_id, session_id, message_ids):
    session = await db.get(Session, session_id)
    if (session is None or session.user_id != user_id or session.workspace_id != workspace_id
            or session.is_deleted or not memory_extraction_eligible(session)):
        raise MemoryAccessDenied("Original user conversation is unavailable")
    access = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
        project_id=extraction_project_id(session))
    removed = {id for payload in (await db.scalars(select(AgentEvent.payload).where(
        AgentEvent.session_id == session_id, AgentEvent.kind == "surface.messages_removed"))).all()
        for id in service._removed_ids(payload)}
    sources = []
    for message_id in dict.fromkeys(message_ids):
        message = await db.get(Message, message_id)
        if (message is None or message.role != "user" or message.user_id != user_id
                or message.session_id != session_id or message_id in removed):
            raise MemoryAccessDenied("Original user message is unavailable")
        parts = (await db.scalars(select(Part).where(Part.message_id == message_id,
            Part.user_id == user_id, Part.session_id == session_id, Part.type == "text").order_by(Part.id))).all()
        found = False
        for part in parts:
            data = part.data or {}
            body = data.get("text")
            if (data.get("origin") != "human" or data.get("origin_ref", {}).get("actor_user_id") != user_id
                    or data.get("synthetic") or data.get("ignored") or not isinstance(body, str) or not body.strip()):
                continue
            if len(body) > 16000 or len(sources) >= 8:
                raise ValueError("Original evidence exceeds the write budget")
            revisions = list((await db.scalars(select(AgentEvent.sequence).where(
                AgentEvent.session_id == session_id, AgentEvent.part_id == part.id,
                AgentEvent.kind.in_(("part.created", "part.updated"))))).all())
            source = {"source_kind": "user_statement", "session_id": session_id, "message_id": message_id,
                "part_id": part.id, "turn_id": message_id, "source_revision": max(revisions, default=1), "body": body,
                "content_hash": sha256(body.encode()).hexdigest(), "occurred_at": message.created_at}
            # Existing tombstones apply even while the original chat is retained.
            from db.models.memory_v2 import MemorySource, MemoryTombstone
            # A forgotten original stays forgotten even if a later extractor
            # assigned a different source snapshot revision to the same part.
            forgotten = await db.scalar(select(MemoryTombstone.id).join(MemorySource,
                MemorySource.id == MemoryTombstone.object_id).where(
                MemoryTombstone.user_id == user_id, MemoryTombstone.object_kind == "source",
                MemorySource.user_id == user_id, MemorySource.part_id == part.id))
            if forgotten:
                raise MemoryAccessDenied("This evidence was forgotten")
            source_id = service.source_snapshot_id(user_id=user_id, workspace_id=workspace_id,
                project_id=access.project_id, data=source)
            if await db.scalar(select(MemoryTombstone.id).where(MemoryTombstone.user_id == user_id,
                    MemoryTombstone.object_kind == "source", MemoryTombstone.object_id == source_id)):
                raise MemoryAccessDenied("This evidence was forgotten")
            old = await db.get(MemorySource, source_id)
            if old is not None and not await service.source_body_is_available(db, access, old):
                raise MemoryAccessDenied("This evidence is no longer readable")
            sources.append(source)
            found = True
        if not found:
            raise MemoryAccessDenied("Only original human text can support a memory")
    if not sources:
        raise ValueError("Original user evidence is required")
    return sources, access


def source_refs(sources):
    return [{key: source[key] for key in ("session_id", "message_id", "part_id", "source_revision", "content_hash")}
            for source in sources]


async def verify_claim(*, claim, sources, previous=None, account_setting=None, verifier=None):
    from core.config import get_config
    config = get_config()
    item = {"claim": claim, "sources": [mask_sensitive(s["body"]) for s in sources]}
    if account_setting is not None:
        item["account_setting"] = account_setting
    if previous is not None:
        item["previous_memory"] = previous
    verdicts, usage = await asyncio.wait_for((verifier or GroundingVerifier(config.memory)).verify([item],
        purpose="memory_revision" if previous is not None else "memory",
        model=config.memory.extract_model or config.model), timeout=config.memory.write_verify_timeout_seconds)
    return verdicts == [True], usage


async def write_proposal(*, user_id, workspace_id, session_id, message_ids, operation_id,
                         summary, project_id=None, fact_key=None, memory_id=None,
                         expected_revision=None, sensitive=False, verifier=None):
    """Freeze → independently verify outside SQL → recheck → commit one receipt."""
    service._reject_sensitive(summary)
    payload = {"session_id": session_id, "message_ids": list(message_ids), "summary": summary,
        "project_id": project_id, "fact_key": fact_key, "memory_id": memory_id,
        "expected_revision": expected_revision, "sensitive": sensitive}
    async with get_db_session() as db:
        old = await previous_receipt(db, user_id, operation_id, payload)
        if old is not None:
            return old
        sources, source_access = await human_sources(db, user_id=user_id, workspace_id=workspace_id,
            session_id=session_id, message_ids=message_ids)
        target = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=bool(memory_id))
        previous = None
        if memory_id:
            row = await db.scalar(select(UserMemory).where(UserMemory.id == memory_id, *target.predicates(UserMemory)))
            if row is None or not service._live(row):
                raise MemoryAccessDenied("Memory is unavailable")
            expected_revision = row.revision if expected_revision is None else expected_revision
            previous = service._summary(row.value)
            project_id = row.project_id
    claim = (f"Proposed record {'for the requested project' if project_id else 'for the personal collection'}. "
             f"Determine eligibility and lifetime from the original sources, not this proposal. "
             f"Preserve stated subjects and conditions; ownership does not make the owner the subject: {summary}")
    supported, usage = await verify_claim(claim=claim, sources=sources, previous=previous, verifier=verifier)
    async with get_db_session() as db:
        await db.scalar(select(Session).where(Session.id == session_id).with_for_update())
        await service.lock_memory_authority(db, user_id=user_id)
        old = await previous_receipt(db, user_id, operation_id, payload)
        if old is not None:
            return old
        current_sources, current_access = await human_sources(db, user_id=user_id, workspace_id=workspace_id,
            session_id=session_id, message_ids=message_ids)
        if source_refs(current_sources) != source_refs(sources) or current_access != source_access:
            raise service.MemoryConflict("Original evidence changed while verifying")
        target = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=bool(memory_id))
        from memory.settings import saving_paused_locked
        result = {"state": "needs_context", "reason": "claim_not_supported", "instruction":
                  "Nothing was saved. Clarify the fact, subject or lasting scope; do not say it is remembered."}
        if await saving_paused_locked(db, user_id, session_id):
            result = {"state": "paused", "instruction": "Memory saving is paused; nothing was saved."}
        elif supported:
            if memory_id:
                row = await service.edit_note_in_session(db, access=target, memory_id=memory_id, summary=summary,
                    expected_revision=expected_revision, request_id=operation_id,
                    verified_sources=(current_sources, current_access))
                if row is None:
                    raise service.MemoryConflict("Memory is no longer current")
                result = {"state": "saved", "memory_id": row["id"], "revision": row["revision"], "summary": row["summary"]}
            else:
                proposal = {"summary": summary, "type": "PREFERENCE" if (fact_key or "").startswith("personal.style.") else "USER_NOTE",
                            "quotes": [], "source_indexes": list(range(len(sources)))}
                row = await service.create_candidate_in_session(db, access=target, source_access=current_access,
                    summary=summary, type=proposal["type"], sources=current_sources, fact_key=fact_key,
                    evidence={"origin": "conversation_proposal", "operation_id": operation_id},
                    idempotency_key=operation_id)
                if row is None:
                    result = {"state": "rejected", "reason": "forgotten_or_declined"}
                elif service._summary(row.value) != summary:
                    result = {"state": "conflict", "reason": "fact_already_exists", "memory_id": row.id, "revision": row.revision}
                else:
                    if not sensitive:
                        from memory.grounding import admit_verified_memory
                        await admit_verified_memory(db, target, row, job_id=operation_id, proposal=proposal,
                            sources=current_sources, source_access=current_access)
                    result = {"state": "needs_confirmation" if row.status == "CANDIDATE" else "saved",
                        "memory_id": row.id, "revision": row.revision, "summary": service._summary(row.value)}
        return await record_receipt(db, user_id=user_id, operation_id=operation_id, kind="memory",
            payload=payload, result=result, source_refs=source_refs(sources),
            occurred_at=max(service._utc(s["occurred_at"]) for s in sources))


async def write_profile_proposal(*, user_id, workspace_id, session_id, message_ids, operation_id,
                                 patch, verifier=None, before_commit=None):
    from db.models.preference import UserPreference
    payload = {"patch": patch, "session_id": session_id, "message_ids": list(message_ids)}
    decision_id = operation_id + ":proposal"
    async with get_db_session() as db:
        old = await previous_receipt(db, user_id, decision_id, payload)
        if old is not None:
            return old
        sources, access = await human_sources(db, user_id=user_id, workspace_id=workspace_id,
            session_id=session_id, message_ids=message_ids)
        extra = await db.scalar(select(UserPreference.extra).where(UserPreference.user_id == user_id)) or {}
        revision = int(extra.get(REVISION_KEY) or 0)
    # Use the same typed claim as extraction. A prose wrapper about every
    # possible field made a narrow language update look like a broader claim,
    # while extraction of the identical source could accept it later.
    from assistant.style import SETTABLE_STYLE, setting_claim
    fact_keys = {field: key for key, field in SETTABLE_STYLE.items()}
    claims = [setting_claim(fact_keys[key], "on" if value is True else "off" if value is False else value)
              for key, value in patch.items()]
    supported, usage = await verify_claim(claim="; ".join(claims), account_setting=patch,
        sources=sources, verifier=verifier)
    async with get_db_session() as db:
        await db.scalar(select(Session).where(Session.id == session_id).with_for_update())
        await service.lock_memory_authority(db, user_id=user_id)
        old = await previous_receipt(db, user_id, decision_id, payload)
        if old is not None:
            return old
        if before_commit:
            await before_commit(db)
        current, current_access = await human_sources(db, user_id=user_id, workspace_id=workspace_id,
            session_id=session_id, message_ids=message_ids)
        if source_refs(current) != source_refs(sources) or current_access != access:
            raise service.MemoryConflict("Original evidence changed")
        if supported:
            receipt = await apply_profile(db, user_id=user_id, patch=patch, operation_id=operation_id, via="chat",
                expected_revision=revision, occurred_at=max(service._utc(s["occurred_at"]) for s in sources),
                source_refs=source_refs(sources), checked_source=True)
        else:
            receipt = {"state": "needs_context", "instruction": "No settings changed. Do not say saved, remembered or updated. "
                "Clarify the user, fields or lasting scope; do not repeat the same proposal."}
        receipt["verification"] = {key: usage[key] for key in (
            "grounding_policy_version", "unsupported_count", "intent_counts", "setting_scope_counts") if key in usage}
        receipt = await record_receipt(db, user_id=user_id, operation_id=decision_id, kind="profile_proposal",
            payload=payload, result=receipt, source_refs=source_refs(sources))
    from assistant.profile import load_view
    from bus import bus
    from bus.events import ASSISTANT_PROFILE_UPDATED
    try:
        bus.publish(ASSISTANT_PROFILE_UPDATED, {"userId": user_id, "profile": await load_view(user_id)})
    except Exception:
        # The committed receipt stays authoritative even if UI delivery fails.
        from core.log import create_logger
        create_logger("memory.commands").warning("Profile committed; live notification unavailable")
    return receipt
