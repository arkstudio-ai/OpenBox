"""Every export format consumes one freshly authorized, bounded SQL snapshot."""
from sqlalchemy import select

from db.base import get_db_session
from db.models.memory_wiki import MemoryWikiPage
from db.models.wiki_exchange import WikiExchangeBundle, WikiExchangeDocument
from db.models.wiki_platform import WikiRelation
from memory.policy import resolve_access_scope
from memory.service import lock_memory_authority
from memory.wiki import organization, service
from wiki_compiler.export import render_export
from wiki_compiler.hashing import canonical_hash


async def snapshot(*, user_id, workspace_id, project_id=None):
    async with get_db_session() as db:
        # Serialize against source corrections/forgetting for the whole snapshot.
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=project_id is None)
        rows = list((await db.scalars(select(MemoryWikiPage).where(*scope.predicates(MemoryWikiPage),
            MemoryWikiPage.status == "PUBLISHED", MemoryWikiPage.deleted_at.is_(None))
            .order_by(MemoryWikiPage.id).limit(501))).all())
        if len(rows) > 500:
            raise service.WikiStateError("wiki_exchange_export_limit")
        result = {"schema_version": "openbox-wiki-1", "pages": [], "sources": {}, "concepts": [],
                  "relations": [], "foreign_bundles": {}, "profiles": [], "workflows": []}
        for page in rows:
            local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=page.project_id)
            view = await service._page_view(db, local, page)
            if not view["body_available"]:
                continue
            sources = await service.read_sources(db, local, page.source_manifest, page.memory_manifest, acl_epoch=page.acl_epoch)
            for source in sources.values():
                result["sources"][source.id] = {"id": source.id, "revision": source.source_revision,
                    "body": source.body, "kind": source.source_kind, "content_hash": source.content_hash}
            document = await db.get(WikiExchangeDocument, page.candidate_id)
            if document and document.status == "APPROVED" and document.page_id == page.id:
                bundle = await db.get(WikiExchangeBundle, document.bundle_id)
                view["foreign"] = {"bundle_id": bundle.id, "path": document.path, "frontmatter": document.frontmatter}
                attachments = {source.source_metadata["path"]: source.body for source in sources.values()
                               if source.source_metadata.get("path") in bundle.attachments
                               and source.source_metadata["path"] != "index.md"}
                result["foreign_bundles"][bundle.id] = {"manifest": bundle.manifest, "attachments": attachments}
            result["pages"].append(view)
        # Include valid concepts even when their page is awaiting publication.
        from db.models.wiki_platform import WikiConcept
        concepts = list((await db.scalars(select(WikiConcept).where(*scope.predicates(WikiConcept),
            WikiConcept.status == "ACTIVE").order_by(WikiConcept.id).limit(1001))).all())
        if len(concepts) > 1000:
            raise service.WikiStateError("wiki_exchange_export_limit")
        for concept in concepts:
            local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=concept.project_id)
            if await organization.current_bindings(db, local, concept):
                result["concepts"].append(organization.catalog_entry(concept) | {"page_id": concept.page_id,
                    "description": concept.description})
        concept_ids = {item["id"] for item in result["concepts"]}
        relations = list((await db.scalars(select(WikiRelation).where(*scope.predicates(WikiRelation),
            WikiRelation.status == "ACTIVE").order_by(WikiRelation.id).limit(2001))).all())
        if len(relations) > 2000:
            raise service.WikiStateError("wiki_exchange_export_limit")
        for relation in relations:
            local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=relation.project_id)
            if (relation.from_id in concept_ids and relation.to_id in concept_ids
                    and await service.dependencies_current(db, local, relation)):
                result["relations"].append({"id": relation.id, "type": relation.relation_type,
                    "from": relation.from_id, "to": relation.to_id, "attributes": relation.attributes,
                    "evidence": relation.evidence})
        from memory.wiki.profiles import exchange_projection
        projection = await exchange_projection(db, scope)
        result["relations"].extend(projection.pop("relations"))
        result.update(projection)
        result["snapshot_hash"] = canonical_hash(result)
        return result


async def export(*, user_id, workspace_id, project_id=None, format="okf"):
    return render_export(await snapshot(user_id=user_id, workspace_id=workspace_id, project_id=project_id), format)
