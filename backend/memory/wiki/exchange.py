"""Review-first OKF imports; foreign documents never grant local authority."""
import re

from sqlalchemy import select

from core import config as runtime_config
from core.identifier import ascending
from db.base import get_db_session
from db.models.memory_v2 import MemorySource
from db.models.memory_wiki import MemoryWikiDependency, MemoryWikiPage
from db.models.wiki_exchange import WikiExchangeBundle, WikiExchangeDocument
from memory.policy import resolve_access_scope
from memory.service import lock_memory_authority
from memory.wiki import service
from memory.wiki.organization import require_enabled, scoped_values
from wiki_compiler.exchange import parse_okf
from wiki_compiler.hashing import canonical_hash, text_hash
from wiki_compiler.organization import concept_slug


def intact(document):
    return document.content_hash == canonical_hash({"frontmatter": document.frontmatter, "body": document.body})


async def read_import_dependency(db, scope, reference, *, lock=False):
    stmt = select(WikiExchangeDocument).where(WikiExchangeDocument.id == reference["id"],
        *scope.predicates(WikiExchangeDocument))
    document = await db.scalar(stmt.with_for_update() if lock else stmt)
    if (document is None or document.status != "APPROVED" or not intact(document)
            or document.revision != reference["revision"] or document.content_hash != reference["content_hash"]
            or reference["source_ids"] != document.source_ids):
        raise service.WikiStateError("wiki_exchange_source_changed")
    return document


async def source_is_reviewed(db, scope, source_id):
    documents = list((await db.scalars(select(WikiExchangeDocument).where(*scope.predicates(WikiExchangeDocument),
        WikiExchangeDocument.status == "APPROVED"))).all())
    return any(source_id in document.source_ids and intact(document) for document in documents)


async def preview(*, user_id, workspace_id, project_id, data):
    parsed = parse_okf(data)
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        domain = service.domain_for(scope, project_id)
        bundle = await db.scalar(select(WikiExchangeBundle).where(WikiExchangeBundle.domain == domain,
            WikiExchangeBundle.content_hash == parsed["content_hash"]))
        if bundle is None:
            bundle = WikiExchangeBundle(id=ascending("wiki_import"), **scoped_values(scope), domain=domain,
                content_hash=parsed["content_hash"], manifest=parsed["manifest"], attachments=parsed["attachments"],
                warnings=parsed["warnings"])
            db.add(bundle)
            for item in parsed["documents"]:
                raw_slug = item["frontmatter"].get("x-openbox", {}).get("slug") if isinstance(item["frontmatter"].get("x-openbox"), dict) else None
                slug = raw_slug if isinstance(raw_slug, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", raw_slug) else concept_slug(item["title"])
                db.add(WikiExchangeDocument(id=ascending("wiki_import_doc"), **scoped_values(scope),
                    bundle_id=bundle.id, **item, slug=slug, status="PENDING"))
            await db.flush()
        return await bundle_view(db, scope, bundle)


async def document_view(db, scope, document):
    identity = service.target_identity(scope, document.slug, document.project_id)
    existing = await service._target(db, scope, identity)
    unavailable = False
    if document.status == "APPROVED":
        page = await db.get(MemoryWikiPage, document.page_id)
        unavailable = not page or not (await service._page_view(db, scope, page))["body_available"]
    return {"id": document.id, "revision": document.revision, "content_hash": document.content_hash,
            "path": document.path, "title": document.title if not unavailable else None,
            "slug": document.slug, "type": document.frontmatter.get("type"),
            "body": document.body if not unavailable else None, "frontmatter": document.frontmatter if not unavailable else {},
            "status": "stale" if unavailable else document.status.lower(), "page_id": document.page_id,
            "conflict": bool(existing and existing.id != document.page_id), "conflicting_page_id": existing.id if existing else None}


async def bundle_view(db, scope, bundle):
    rows = list((await db.scalars(select(WikiExchangeDocument).where(
        WikiExchangeDocument.bundle_id == bundle.id, *scope.predicates(WikiExchangeDocument)).order_by(WikiExchangeDocument.path))).all())
    return {"id": bundle.id, "revision": bundle.revision, "content_hash": bundle.content_hash,
            "project_id": bundle.project_id, "warnings": bundle.warnings,
            "documents": [await document_view(db, scope, row) for row in rows],
            "publication_requires_approval": True, "foreign_history_executable": False}


async def list_bundles(*, user_id, workspace_id, project_id=None, offset=0):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=project_id is None)
        rows = list((await db.scalars(select(WikiExchangeBundle).where(*scope.predicates(WikiExchangeBundle))
            .order_by(WikiExchangeBundle.created_at.desc(), WikiExchangeBundle.id).offset(offset).limit(21))).all())
        return {"bundles": [{"id": row.id, "project_id": row.project_id, "created_at": row.created_at.isoformat()}
                            for row in rows[:20]], "next_offset": offset + 20 if len(rows) > 20 else None}


async def detail(*, user_id, workspace_id, bundle_id):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        bundle = await db.scalar(select(WikiExchangeBundle).where(WikiExchangeBundle.id == bundle_id,
            *scope.predicates(WikiExchangeBundle)))
        if bundle is None:
            return None
        local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=bundle.project_id)
        return await bundle_view(db, local, bundle)


async def reader_links(db, scope, page, sources):
    document = await db.get(WikiExchangeDocument, page.candidate_id)
    if not document or document.page_id != page.id or document.status != "APPROVED":
        return {}
    links = {source.source_metadata["path"]: {"source_id": source.id}
             for source in sources.values() if source.source_metadata.get("path")}
    rows = list((await db.scalars(select(WikiExchangeDocument).where(
        WikiExchangeDocument.bundle_id == document.bundle_id, WikiExchangeDocument.status == "APPROVED",
        *scope.predicates(WikiExchangeDocument)))).all())
    for target in rows:
        linked = await db.get(MemoryWikiPage, target.page_id)
        if linked and (await service._page_view(db, scope, linked))["body_available"]:
            links[target.path] = {"page_id": linked.id}
    return {"exchange_path": document.path, "exchange_links": links}


async def decide(*, user_id, workspace_id, document_id, expected_revision, content_hash, action, slug=None,
                 acknowledge_warnings=False):
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        document = await db.scalar(select(WikiExchangeDocument).where(WikiExchangeDocument.id == document_id,
            *scope.predicates(WikiExchangeDocument)).with_for_update())
        if document is None:
            return None
        local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=document.project_id)
        if expected_revision != document.revision or content_hash != document.content_hash or not intact(document):
            raise service.WikiStateError("wiki_exchange_document_changed")
        if document.status != "PENDING":
            raise service.WikiStateError("wiki_exchange_document_decided")
        if action == "reject":
            document.status, document.revision, document.updated_at = "REJECTED", document.revision + 1, service.now()
        elif action == "rename":
            if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", slug):
                raise service.WikiStateError("invalid_wiki_target")
            document.slug, document.revision, document.updated_at = slug, document.revision + 1, service.now()
        elif action == "approve":
            bundle = await db.get(WikiExchangeBundle, document.bundle_id)
            if bundle.warnings and not acknowledge_warnings:
                raise service.WikiStateError("wiki_exchange_warnings_require_review")
            require_enabled(runtime_config.get_config().memory, user_id)
            await publish(db, local, document)
        else:
            raise service.WikiStateError("wiki_exchange_invalid_action")
        await db.flush()
        return await document_view(db, local, document)


async def publish(db, scope, document):
    identity = service.target_identity(scope, document.slug, document.project_id)
    if await service._target(db, scope, identity):
        raise service.WikiStateError("wiki_exchange_target_conflict")
    # The imported document is the source. Foreign citations remain metadata;
    # approval never asserts that the foreign source paths were locally verified.
    instant = service.now()
    source = MemorySource(id=ascending("memory_source"), user_id=scope.user_id, workspace_id=scope.workspace_id,
        project_id=scope.project_id, source_revision=1, source_kind="wiki_import", content_hash=text_hash(document.body),
        body=document.body, source_metadata={"bundle_id": document.bundle_id, "path": document.path,
        "import_document_id": document.id}, acl_epoch=scope.acl_epoch, status="ACTIVE", created_at=instant)
    db.add(source)
    sources = [source]
    bundle = await db.get(WikiExchangeBundle, document.bundle_id)
    # Bundle assets (including logs) are immutable sources. Re-export never
    # bypasses forgetting by copying an unchecked attachment from the bundle.
    for path, body in sorted(bundle.attachments.items()):
        if path == "index.md":
            continue
        source_id = "wiki_ref_" + canonical_hash({"bundle": bundle.id, "path": path})[:40]
        reference = await db.get(MemorySource, source_id)
        if reference is None:
            reference = MemorySource(id=source_id, user_id=scope.user_id, workspace_id=scope.workspace_id,
                project_id=scope.project_id, source_revision=1, source_kind="wiki_import", content_hash=text_hash(body),
                body=body, source_metadata={"bundle_id": bundle.id, "path": path}, acl_epoch=scope.acl_epoch,
                status="ACTIVE", created_at=instant)
            db.add(reference)
        elif not await service.source_body_is_available(db, scope, reference):
            raise service.WikiStateError("wiki_exchange_source_changed")
        sources.append(reference)
    document.status, document.revision, document.updated_at = "APPROVED", document.revision + 1, instant
    document.source_id, document.page_id = source.id, "wiki_" + identity[:40]
    document.source_ids = [item.id for item in sources]
    refs = [service._source_ref(item, scope) for item in sources]
    dependency = {"kind": "exchange", "id": document.id, "revision": document.revision,
                  "content_hash": document.content_hash, "source_ids": document.source_ids}
    page = MemoryWikiPage(id=document.page_id, target_identity=identity, user_id=scope.user_id,
        workspace_id=scope.workspace_id, project_id=scope.project_id, slug=document.slug, title=document.title,
        revision=1, content_hash=text_hash(document.body), body=document.body, status="PUBLISHED",
        source_manifest=refs, memory_manifest=[dependency], paragraphs=[{"text": document.body,
        "citations": [{"source_id": source.id, "revision": 1, "content_hash": source.content_hash,
                       "quote": document.body[:2400]}]}], acl_epoch=scope.acl_epoch,
        policy_version="reviewed-okf-import-v1", model="none", input_hash=document.content_hash,
        candidate_id=document.id, created_at=instant, updated_at=instant)
    db.add(page)
    for item in [*refs, dependency]:
        db.add(MemoryWikiDependency(id=ascending("wiki_dep"), page_id=page.id, page_revision=1,
            object_kind=item["kind"], object_id=item["id"], object_revision=item["revision"], content_hash=item["content_hash"]))
    await db.flush()
    await service.enqueue_page_outbox(db, page, runtime_config.get_config().memory)
