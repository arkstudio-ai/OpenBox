"""Bounded SQL-authorized adapters; model arguments never supply authority."""
from sqlalchemy import select, or_

from db.base import get_db_session
from memory.policy import MemoryAccessDenied, resolve_access_scope
from memory.presentation import model_item
from memory.redaction import redact_credentials


async def authority(db, turn):
    return await resolve_access_scope(db, user_id=turn.user_id, workspace_id=turn.workspace_id,
        project_id=turn.project_id, include_all_projects=turn.include_all_projects,
        reading_session_id=turn.session_id)


async def read_request(request, turn, config):
    if turn.read_policy == "documents_only" and request.kind != "settings":
        from memory.document_scope import read_documents
        return await read_documents(request, turn, config)
    if request.kind in {"settings", "search"}:
        async with get_db_session() as db:
            await authority(db, turn)
            if request.kind == "settings":
                from memory.packet import current_settings
                return {"kind": "settings", "status": "complete", "settings": await current_settings(db, turn.user_id)}
    if request.kind == "search":
        from memory.retrieval import search_memory
        result = await search_memory(query=request.query or turn.query, user_id=turn.user_id,
            workspace_id=turn.workspace_id, project_id=turn.project_id,
            include_all_projects=turn.include_all_projects, config=config, limit=config.recall_item_limit,
            time_query=turn.query, time_intent=turn.time_intent,
            io_budget_seconds=max(.01, config.read_more_deadline_seconds - .2))
        async with get_db_session() as db:
            await authority(db, turn)
        return {"kind": "search", "status": "partial" if result.get("degraded_reasons") else "complete",
            "items": [model_item(item) for item in result["items"]],
            "time_context": result.get("time_context", {}),
            "degraded_reasons": result.get("degraded_reasons", []), "usage": result.get("usage", {})}
    async with get_db_session() as db:
        scope = await authority(db, turn)
        if request.kind == "sources":
            from memory.service import read_source_in_scope
            items = []
            for ref in request.refs[:8]:
                value = await read_source_in_scope(db, access=scope, source_id=ref["id"], source_revision=ref["revision"])
                if not value or not value.get("available", value.get("body_available", False)):
                    items.append({"id": ref["id"], "revision": ref["revision"], "available": False})
                    continue
                body = value.pop("body", "") or ""
                end = min(len(body), request.offset + request.max_chars)
                items.append({**value, "id": ref["id"], "revision": ref["revision"], "available": True,
                    "text": redact_credentials(body[request.offset:end]), "offset": request.offset,
                    "next_offset": end if end < len(body) else None, "total_chars": len(body)})
            return {"kind": request.kind, "status": "complete", "items": items}
        if request.kind == "documents":
            from db.models.memory_document import MemoryDocument
            rows = list((await db.scalars(select(MemoryDocument).where(*scope.predicates(MemoryDocument),
                MemoryDocument.status != "DELETED")
                .order_by(MemoryDocument.id).offset(request.offset).limit(21))).all())
            return {"kind": request.kind, "status": "complete", "items": [
                {"id": row.id, "revision": row.revision, "filename": row.filename, "project_id": row.project_id,
                 "status": row.status, "source_ids": row.source_ids, "page_ids": row.page_ids} for row in rows[:20]],
                "next_offset": request.offset + 20 if len(rows) > 20 else None}
        if request.kind == "assets":
            from assets.service import resource_query
            from db.models.file_asset import FileAsset
            from memory.presentation import iso_time
            stmt = resource_query(turn.user_id, turn.workspace_id, owned_only=True).where(*scope.predicates(FileAsset))
            if request.query:
                stmt = stmt.where(or_(FileAsset.name.icontains(request.query, autoescape=True),
                                      FileAsset.description.icontains(request.query, autoescape=True)))
            rows = list((await db.scalars(stmt.order_by(FileAsset.created_at.desc(), FileAsset.id)
                .offset(request.offset).limit(11))).all())
            return {"kind": request.kind, "status": "complete", "items": [
                {**{key: getattr(row, key) for key in ("id", "name", "description", "description_state",
                    "project_id", "session_id", "mime")},
                 "created_at": iso_time(row.created_at),
                 "description_updated_at": iso_time(row.description_updated_at)} for row in rows[:10]],
                "next_offset": request.offset + 10 if len(rows) > 10 else None,
                "instruction": "Metadata identifies assets; it does not authorize editing, payment or publishing."}
        if request.kind == "wiki":
            from memory.wiki.service import authorized_wiki_documents
            from memory.presentation import document_item
            wanted = {(ref["id"], ref["revision"]) for ref in request.refs}
            rows = await authorized_wiki_documents(db, scope, config,
                only={("wiki", ref["id"]) for ref in request.refs})
            return {"kind": request.kind, "status": "complete", "items": [model_item(document_item(row))
                for row in rows if (row.id, row.revision) in wanted]}
        if request.kind == "experience":
            from memory.experiences import read_enabled
            return await read_enabled(db, scope, query=request.query, offset=request.offset, refs=request.refs)
        if request.kind == "history":
            items = [await history_window(db, scope, turn, ref["id"], offset=request.offset,
                max_chars=request.max_chars) for ref in request.refs[:2]]
            return {"kind": request.kind, "status": "complete", "items": items}
        if request.kind == "task_state":
            from memory.retrieval import read_task_state
            # Task state is current SQL data. The existing assistant/voice task
            # adapters additionally expose watched tasks and card identities.
            return {"kind": request.kind, "status": "complete", **await read_task_state(scope,
                session_id=request.refs[0]["id"] if request.refs else None)}
    raise ValueError("Unknown memory read kind")


async def history_window(db, scope, turn, session_id, *, offset=0, limit=10, max_chars=3000,
                         visible_only=False, live_execution=False):
    """One owner-bound window, shared by supplements and the voice view adapter.

    Both paths exclude removed messages and forgotten source parts. A visible
    voice window also excludes internal reasoning/tool traces from the chat.
    """
    from db.models.session import Session
    from db.models.message import Message
    from db.models.agent_event import AgentEvent
    from db.models.part import Part
    from db.models.memory_v2 import MemorySource, MemoryTombstone
    from memory.service import _removed_ids
    from assistant.history import _window
    from memory.session_policy import memory_extraction_eligible
    row = await db.scalar(select(Session).where(Session.id == session_id,
        *scope.predicates(Session, personal_visibility=False), Session.is_deleted.is_(False)))
    eligible = row is not None and memory_extraction_eligible(row)
    if (not eligible and row is not None and live_execution and visible_only
            and turn.entry == "voice" and turn.read_policy == "allowed"
            and row.kind == "normal" and row.memory_policy == "assistant_isolated" and not row.parent_id):
        # Voice can inspect its own assistant's current execution conversation
        # without making that task eligible for extraction or memory retrieval.
        # Explicit user isolation ("isolated") is never bypassed. Generic memory
        # tools cannot select this internal live-read path.
        from db.models.assistant import AssistantTask
        eligible = await db.scalar(select(AssistantTask.id).where(
            AssistantTask.execution_session_id == row.id, AssistantTask.assistant_session_id == turn.session_id,
            AssistantTask.user_id == turn.user_id, AssistantTask.workspace_id == turn.workspace_id,
            AssistantTask.archived_at.is_(None))) is not None
    if not eligible:
        raise MemoryAccessDenied("History is unavailable")
    removed = {identity for payload in (await db.scalars(select(AgentEvent.payload).where(
        AgentEvent.session_id == row.id, AgentEvent.user_id == turn.user_id,
        AgentEvent.kind == "surface.messages_removed"))).all() for identity in _removed_ids(payload)}
    forgotten = select(MemorySource.part_id).join(MemoryTombstone,
        MemoryTombstone.object_id == MemorySource.id).where(MemorySource.user_id == turn.user_id,
        MemoryTombstone.user_id == turn.user_id, MemoryTombstone.object_kind == "source",
        MemorySource.part_id.is_not(None))
    visible_message = ()
    if visible_only:
        from sqlalchemy import and_, func
        from assistant.permission_requests import json_text
        channel, body = json_text(db, Part.data, "channel"), json_text(db, Part.data, "text")
        # Tool/reasoning-only steps are not conversation turns. Applying the
        # window limit before filtering them can hide the human's latest edit
        # while the agent is working, even though it is visible on screen.
        visible_message = (select(Part.id).where(Part.message_id == Message.id,
            Part.session_id == row.id, Part.user_id == turn.user_id, Part.id.not_in(forgotten),
            or_(channel.is_(None), channel == "final"),
            or_(Part.type == "file", and_(Part.type == "text",
                func.length(func.trim(func.coalesce(body, ""))) > 0))).exists(),)
    ids = list((await db.scalars(select(Message.id).where(Message.session_id == row.id,
        Message.user_id == turn.user_id, Message.id.not_in(removed), *visible_message)
        .order_by(Message.id.desc()).offset(offset).limit(limit + 1))).all())
    parts = list((await db.scalars(select(Part).where(Part.message_id.in_(ids[:limit]),
        Part.user_id == turn.user_id, Part.id.not_in(forgotten)))).all())
    allowed_parts = {part.id for part in parts if not visible_only or
        (part.type in {"text", "file"} and (part.data or {}).get("channel") in {None, "final"})}
    entries, version = await _window(db, ids=list(reversed(ids[:limit])), session_id=row.id,
        user_id=turn.user_id, main_id=turn.session_id, workspace_id=turn.workspace_id,
        allowed_parts=allowed_parts, include_tools=False, strict=False)
    room = max_chars
    # The viewed window prioritizes the latest complete statements; never
    # truncate a sentence and accidentally discard its qualification.
    for entry in reversed(entries) if visible_only else entries:
        text = entry.get("text", "")
        if len(text) > room:
            entry.update(text="", omitted="evidence_budget", total_chars=len(text))
        else:
            room -= len(text)
    return {"session_id": row.id, "project_id": row.project_id, "version": version,
        "entries": entries, "next_offset": offset + limit if len(ids) > limit else None}


async def catalogs(turn, needs, config, *, recall_needed=False):
    """Small discovery records, selected semantically by the router, not trigger words."""
    from db.models.session import Session
    from memory.packet import ReadRequest
    if turn.read_policy == "documents_only":
        result = await read_request(ReadRequest(kind="documents"), turn, config)
        return [{"kind": "document", **item} for item in result.get("items", [])]
    assets_needed = needs.get("assets_useful", 0) >= .4
    # Uploads have two authoritative stores: FileAsset media and ingested
    # MemoryDocument text. Showing only recent media biases a vague spoken
    # file reference toward a photo even when the document exists.
    documents_needed = assets_needed or needs.get("knowledge_useful", 0) >= .4
    history_needed = needs.get("history_useful", 0) >= .4
    experience_requested = needs.get("experience_useful", 0) >= .4
    # The router sees the request, not the owner's saved procedures. Once it
    # permits and requests memory, a low type score cannot prove no relevant
    # learned method exists. Discover at most two matching titles/conditions
    # locally; keep full procedure reads, authorization and budgets unchanged.
    discover_experience = recall_needed and turn.read_policy == "allowed"
    experience_needed = (experience_requested or discover_experience) and config.enabled("experience_learning", turn.user_id)
    # A semantic skip returns no personal content. Avoid opening an unrelated
    # authorization transaction when there is no catalogue read to authorize.
    if not (assets_needed or documents_needed or history_needed or experience_needed):
        return []
    items = []
    if assets_needed:
        assets = await read_request(ReadRequest(kind="assets"), turn, config)
        items += [{"kind": "asset", **item} for item in assets["items"][:4]]
    if not (documents_needed or history_needed or experience_needed):
        return items
    async with get_db_session() as db:
        scope = await authority(db, turn)
        if documents_needed:
            from db.models.memory_document import MemoryDocument
            rows = list((await db.scalars(select(MemoryDocument).where(*scope.predicates(MemoryDocument),
                MemoryDocument.status != "DELETED")
                .order_by(MemoryDocument.created_at.desc(), MemoryDocument.id).limit(2))).all())
            documents = [{"kind": "document", "id": row.id, "revision": row.revision,
                "filename": row.filename, "project_id": row.project_id, "status": row.status,
                "instruction": "Recent discovery candidate, not a resolved reference. Clarify ambiguous file identity; "
                    "read its authorized sources before making claims about its contents."} for row in rows]
            # Keep the same four-entry discovery allowance, with both stores
            # represented. Do not inflate every voice turn's context budget.
            if documents:
                items = documents + items[:2]
        if history_needed:
            rows = list((await db.scalars(select(Session).where(*scope.predicates(Session, personal_visibility=False),
                Session.is_deleted.is_(False), Session.kind == "normal", Session.parent_id.is_(None),
                Session.memory_policy == "standard").order_by(Session.updated_at.desc()).limit(6))).all())
            items += [{"kind": "history", "id": row.id, "title": row.title, "project_id": row.project_id,
                "updated_at": row.updated_at.isoformat(), "instruction": "Read this session with memory_read_more before making factual claims about its history."}
                for row in rows]
        if experience_needed:
            from memory.experiences import read_enabled
            result = await read_enabled(db, scope, query=turn.query, matched_only=not experience_requested)
            items += [{"kind": "experience", "id": item["id"], "revision": item["revision"],
                "skill_id": item["skill_id"], "title": item["content"]["title"],
                "conditions": item["content"]["conditions"],
                "instruction": "Read the procedure and its failure boundaries before applying it; this is not execution authority."}
                for item in result["items"][:4 if experience_requested else 2]]
    return items


async def revalidate_result(arguments, value, turn, config):
    """No provider calls, no new search and no consumption of a second read batch."""
    from memory.packet import ReadRequest
    from memory.retrieval import authorized_documents
    from memory.presentation import document_item
    from memory.redaction import json_hash
    from memory.read_contract import permits
    if not all(permits(turn, request["kind"], bounded=True) for request in arguments["reads"]):
        raise MemoryAccessDenied("The user restricted this read")
    async with get_db_session() as db:
        scope = await authority(db, turn)
        for request, result in zip(arguments["reads"], value.get("results", []), strict=False):
            if request["kind"] == "search" and turn.read_policy != "documents_only":
                wanted = {(item["kind"], item["id"]): item for item in result.get("items", [])}
                docs = await authorized_documents(db, scope, config, only=set(wanted))
                current = {(doc.kind, doc.id): model_item(document_item(doc)) for doc in docs}
                if current != wanted:
                    return {"status": "unavailable", "reason": "evidence_changed", "items": []}
            else:
                fresh = await read_request(ReadRequest(**request), turn, config)
                if json_hash({k: v for k, v in fresh.items() if k != "observed_at"}) != json_hash(
                        {k: v for k, v in result.items() if k != "observed_at"}):
                    return {"status": "unavailable", "reason": "evidence_changed", "items": []}
    return value
