"""Read-only Wiki library and provenance projections over current SQL authority.

Inspired by llm-wiki-compiler's viewer, adapted to per-user/project access.
No read calls a model, writes state, or serves a stale source snapshot.
"""
from sqlalchemy import select

from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_wiki import MemoryWikiPage
from memory.policy import active_memory_predicates, resolve_access_scope
from memory.wiki import service


def _summary(page):
    body = page["body"] or ""
    paragraphs = page.get("paragraphs", [])
    excerpt = " ".join(part.get("text", "") for part in paragraphs) or body
    cited = {citation["source_id"] for part in paragraphs for citation in part.get("citations", [])}
    source_ids = [source["id"] for source in page["sources"] if source["id"] in cited]
    return {key: value for key, value in page.items() if key not in {
        "body", "paragraphs", "sources", "memory_dependencies", "content_hash",
    }} | {"excerpt": excerpt[:240], "source_count": len(source_ids), "source_ids": source_ids}


async def library(*, user_id, workspace_id, project_id=None, query="", status="all", offset=0, limit=40):
    """Paginate *validated* results; offset advances over examined rows, not matches.

    Search sees only the same body the reader may see. A source which becomes
    unavailable without a worker event must not leave a searchable old excerpt.
    A bounded scan can return an empty batch with a continuation offset.
    """
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=project_id is None)
        stmt = select(MemoryWikiPage).where(*scope.predicates(MemoryWikiPage),
            MemoryWikiPage.deleted_at.is_(None), MemoryWikiPage.status != "REDIRECT").order_by(MemoryWikiPage.updated_at.desc(), MemoryWikiPage.id)
        rows = list((await db.scalars(stmt.offset(offset).limit(200))).all())
        result, consumed = [], 0
        local_scopes = {}
        for page in rows:
            consumed += 1
            if await service.target_is_deleted(db, page):
                continue
            if page.project_id not in local_scopes:
                local_scopes[page.project_id] = await resolve_access_scope(db, user_id=user_id,
                    workspace_id=scope.workspace_id, project_id=page.project_id)
            view = await service._page_view(db, local_scopes[page.project_id], page)
            uploaded = next((r for r in page.memory_manifest if r["kind"] == "document"), None)
            if uploaded and not query:
                from db.models.memory_document import MemoryDocument
                document = await db.get(MemoryDocument, uploaded["id"])
                if document and document.page_ids and page.id != document.page_ids[0]:
                    continue
            if status != "all" and view["status"] != status:
                continue
            if query.casefold() not in f'{view["title"]}\n{view["body"] or ""}'.casefold():
                continue
            result.append(_summary(view))
            if len(result) >= limit:
                break
        has_more = consumed < len(rows) or len(rows) == 200
        return {"pages": result, "next_offset": offset + consumed if has_more else None}


async def page_detail(*, user_id, workspace_id, page_id):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        page = await db.scalar(select(MemoryWikiPage).where(MemoryWikiPage.id == page_id,
            *scope.predicates(MemoryWikiPage), MemoryWikiPage.deleted_at.is_(None)))
        from memory.wiki.consolidation import redirect_target
        page = await redirect_target(db, scope, page)
        if page is None or await service.target_is_deleted(db, page):
            return None
        local = await resolve_access_scope(db, user_id=user_id, workspace_id=scope.workspace_id,
                                          project_id=page.project_id)
        view = await service._page_view(db, local, page)
        view["source_details"] = []
        if view["body_available"]:
            sources = await service.read_sources(db, local, page.source_manifest, page.memory_manifest,
                                                 acl_epoch=page.acl_epoch)
            view["source_details"] = [{"id": source.id, "revision": source.source_revision,
                "kind": source.source_kind, "body": source.body, "session_id": source.session_id,
                "path": source.source_metadata.get("path") if source.source_kind == "wiki_import" else None,
                "created_at": source.created_at.isoformat()} for source in sources.values()]
            from db.models.memory_v2 import MemorySource
            from memory.service import source_is_available
            for detail in view["source_details"]:
                source = sources[detail["id"]]
                if source.source_kind == "verified_memory_revision":
                    detail["changes"] = []
                    for source_id in source.source_metadata.get("change_source_ids", []):
                        original = await db.get(MemorySource, source_id)
                        if original and await source_is_available(db, local, original):
                            detail["changes"].append({"body": original.body, "session_id": original.session_id})
            from memory.wiki.exchange import reader_links
            view.update(await reader_links(db, local, page, sources))
            uploaded = next((r for r in page.memory_manifest if r["kind"] == "document"), None)
            if uploaded:
                from memory.documents.authority import read_dependency
                document, revision = await read_dependency(db, local, uploaded)
                view["document"] = {"id": document.id, "filename": document.filename,
                    "sections": [{"id": page_id, "title": section["title"]}
                        for page_id, section in zip(document.page_ids, revision.sections)],
                    "warnings": revision.metadata_.get("warnings", [])}
                for detail in view["source_details"]:
                    detail["filename"] = document.filename
                    detail["original_pages"] = sources[detail["id"]].source_metadata.get("original_pages", [])
                    detail["edited"] = bool(sources[detail["id"]].source_metadata.get("edited"))
        return view


async def compile_sources(*, user_id, workspace_id, project_id=None, offset=0, limit=40):
    """List eligible memories using precisely the compilation admission rules.

    Personal selection means personal sources, whereas a selected project also
    permits the user's personal background. This is not the all-project library.
    """
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        rows = list((await db.scalars(select(UserMemory).where(*scope.predicates(UserMemory),
            *active_memory_predicates()).order_by(UserMemory.updated_at.desc(), UserMemory.id)
            .offset(offset).limit(limit + 1))).all())
        result = []
        for memory in rows[:limit]:
            try:
                sources, _ = await service.collect_compile_sources(db, scope, [memory.id])
            except service.WikiStateError:
                continue
            result.append({"id": memory.id, "summary": memory.value.get("summary", ""),
                "revision": memory.revision, "project_id": memory.project_id,
                "source_count": len(sources), "updated_at": memory.updated_at.isoformat(),
                "source_characters": sum(len(source.body) for source in sources.values()),
                "sources": [{"id": source.id, "characters": len(source.body)} for source in sources.values()]})
        return {"memories": result, "next_offset": offset + limit if len(rows) > limit else None}


async def memory_groups(*, user_id, workspace_id, project_id=None):
    from db.models.wiki_platform import WikiConcept
    from memory.wiki.organization import current_bindings
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=project_id is None)
        rows = (await db.scalars(select(WikiConcept).where(*scope.predicates(WikiConcept),
            WikiConcept.status == "ACTIVE").order_by(WikiConcept.updated_at.desc(), WikiConcept.id).limit(200))).all()
        groups = []
        for concept in rows:
            local = await resolve_access_scope(db, user_id=user_id, workspace_id=scope.workspace_id, project_id=concept.project_id)
            bindings = await current_bindings(db, local, concept)
            if bindings:
                groups.append({"id": concept.id, "title": concept.title, "page_id": concept.page_id,
                    "memory_ids": sorted({b.memory_id for b in bindings})})
        return {"groups": groups}
