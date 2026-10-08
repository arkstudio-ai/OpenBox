"""Human concept reconciliation and evidence-bound semantic relation decisions."""
from sqlalchemy import select

from db.base import get_db_session
from db.models.memory_wiki import MemoryWikiPage
from db.models.wiki_platform import WikiConcept, WikiRelation
from memory.policy import resolve_access_scope
from memory.service import lock_memory_authority
from memory.wiki import organization
from memory.wiki.maintenance import request_recheck
from memory.wiki.service import WikiStateError, dependencies_current, enqueue_page_outbox, now


async def merge(*, user_id, workspace_id, source_id, target_id, source_revision, target_revision):
    if source_id == target_id:
        raise WikiStateError("wiki_concept_same_target")
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        rows = list((await db.scalars(select(WikiConcept).where(*scope.predicates(WikiConcept),
            WikiConcept.id.in_([source_id, target_id])).with_for_update())).all())
        by_id = {item.id: item for item in rows}
        if set(by_id) != {source_id, target_id}:
            return None
        source, target = by_id[source_id], by_id[target_id]
        if source.domain != target.domain:
            raise WikiStateError("wiki_concept_scope_mismatch")
        if source.revision != source_revision or target.revision != target_revision:
            raise WikiStateError("wiki_concept_changed")
        local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=source.project_id)
        source_bindings = await organization.current_bindings(db, local, source)
        target_bindings = await organization.current_bindings(db, local, target)
        if not source_bindings or not target_bindings:
            raise WikiStateError("wiki_source_changed")
        aliases = sorted(set([*source.aliases, source.title, *target.aliases]) - {target.title})
        if len(aliases) > 128:
            raise WikiStateError("wiki_organization_alias_limit")
        by_memory = {item.memory_id: item for item in target_bindings}
        for binding in source_bindings:
            current = by_memory.get(binding.memory_id)
            if current:
                current.evidence = [*current.evidence, *[quote for quote in binding.evidence if quote not in current.evidence]]
                binding.status = "MOVED"
            else:
                binding.concept_id = target.id
                by_memory[binding.memory_id] = binding
        target.aliases, target.user_edited = aliases, True
        target.revision, target.updated_at = target.revision + 1, now()
        source.status, source.merged_into, source.revision, source.updated_at = "MERGED", target.id, source.revision + 1, now()
        from core.config import get_config
        for concept in (source, target):
            page = await db.get(MemoryWikiPage, concept.page_id) if concept.page_id else None
            if page and page.status == "PUBLISHED":
                page.status, page.invalidation_reason, page.body, page.paragraphs = "STALE", "concept_merged", None, []
                page.updated_at = now()
                await enqueue_page_outbox(db, page, get_config().memory, "DELETE")
        await request_recheck(db, local)
        return {"source_id": source.id, "target_id": target.id, "revision": target.revision,
                "needs_recompile": True, "memory_count": len(by_memory)}


async def relations(*, user_id, workspace_id, project_id=None, offset=0):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=project_id is None)
        rows = list((await db.scalars(select(WikiRelation).where(*scope.predicates(WikiRelation),
            WikiRelation.from_kind == "concept",
            WikiRelation.status.in_(["ACTIVE", "PROPOSED"])).order_by(WikiRelation.updated_at.desc(), WikiRelation.id)
            .offset(offset).limit(101))).all())
        result = []
        for row in rows[:100]:
            local = await resolve_access_scope(db, user_id=user_id, workspace_id=scope.workspace_id, project_id=row.project_id)
            if not await dependencies_current(db, local, row):
                continue
            source = await db.get(WikiConcept, row.from_id) if row.from_kind == "concept" else None
            target = await db.get(WikiConcept, row.to_id) if row.to_kind == "concept" else None
            if source and not await organization.current_bindings(db, local, source):
                continue
            if target and not await organization.current_bindings(db, local, target):
                continue
            result.append({"id": row.id, "revision": row.revision, "type": row.relation_type, "status": row.status.lower(),
                "from_id": row.from_id, "to_id": row.to_id, "from_title": source.title if source else None,
                "to_title": target.title if target else row.attributes.get("target_label"),
                "from_page_id": await organization.published_concept_page(db, local, source),
                "to_page_id": await organization.published_concept_page(db, local, target),
                "resolved": bool(source and target), "evidence": row.evidence,
                "evidence_hash": row.attributes.get("evidence_hash"), "project_id": row.project_id})
        return {"relations": result, "next_offset": offset + 100 if len(rows) > 100 else None}


async def decide_relation(*, user_id, workspace_id, relation_id, expected_revision, evidence_hash, action):
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        row = await db.scalar(select(WikiRelation).where(WikiRelation.id == relation_id,
            *scope.predicates(WikiRelation)).with_for_update())
        if row is None:
            return None
        if row.revision != expected_revision or row.attributes.get("evidence_hash") != evidence_hash or row.status != "PROPOSED":
            raise WikiStateError("wiki_relation_changed")
        local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=row.project_id)
        if not await dependencies_current(db, local, row):
            raise WikiStateError("wiki_source_changed")
        if action == "approve":
            source, target = await db.get(WikiConcept, row.from_id), await db.get(WikiConcept, row.to_id)
            if not source or not target or not await organization.current_bindings(db, local, source) or not await organization.current_bindings(db, local, target):
                raise WikiStateError("wiki_relation_unresolved")
            row.status = "ACTIVE"
        elif action == "reject":
            row.status = "REJECTED"
        else:
            raise WikiStateError("wiki_relation_invalid_action")
        row.revision, row.updated_at = row.revision + 1, now()
        return {"id": row.id, "revision": row.revision, "status": row.status.lower()}
