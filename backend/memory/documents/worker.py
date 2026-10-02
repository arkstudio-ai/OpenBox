"""Leased parsing and atomic document/chunk/Wiki publication. Restart-safe."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from sqlalchemy import and_, or_, select, update

from core import config as runtime_config
from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.memory_document import MemoryDocument, MemoryDocumentRevision
from db.models.memory_v2 import MemorySource
from db.models.memory_wiki import MemoryWikiDependency, MemoryWikiPage
from memory.documents.parser import DocumentError, split_text
from memory.documents.service import enqueue_source
from memory.documents.storage import document_storage, download_bytes
from memory.policy import MemoryAccessDenied, resolve_access_scope
from memory.service import lock_memory_authority
from memory.wiki.service import _source_ref, enqueue_page_outbox, now, target_identity
from wiki_compiler.hashing import canonical_hash, text_hash

log = create_logger("memory.documents")


@dataclass(frozen=True)
class DocumentLease:
    id: str
    user_id: str
    revision: int
    owner: str
    generation: int


def claimable():
    return or_(and_(MemoryDocument.status.in_(["PENDING", "RETRY"]), MemoryDocument.available_at <= now()),
        and_(MemoryDocument.status == "PARSING", MemoryDocument.lease_until < now()))


async def claim(owner, config):
    if not config.wiki:
        return None
    async with get_db_session() as db:
        stmt = select(MemoryDocument).where(claimable()).order_by(MemoryDocument.available_at, MemoryDocument.id)
        if config.allowed_user_ids:
            stmt = stmt.where(MemoryDocument.user_id.in_(config.allowed_user_ids))
        row = await db.scalar(stmt.limit(1))
        if not row:
            return None
        if row.attempts >= config.max_attempts:
            row.status, row.reason_code, row.lease_until = "FAILED", "document_attempts_exhausted", None
            return None
        generation = row.lease_generation + 1
        result = await db.execute(update(MemoryDocument).where(MemoryDocument.id == row.id,
            MemoryDocument.revision == row.revision, MemoryDocument.lease_generation == row.lease_generation,
            claimable()).values(status="PARSING", lease_owner=owner, lease_generation=generation,
            lease_until=now() + timedelta(seconds=max(90, config.worker_lease_seconds)),
            attempts=MemoryDocument.attempts + 1, updated_at=now()).execution_options(synchronize_session=False))
        return DocumentLease(row.id, row.user_id, row.revision, owner, generation) if result.rowcount == 1 else None


async def live(db, lease, *, lock=False):
    stmt = select(MemoryDocument).where(MemoryDocument.id == lease.id, MemoryDocument.revision == lease.revision,
        MemoryDocument.status == "PARSING", MemoryDocument.lease_owner == lease.owner,
        MemoryDocument.lease_generation == lease.generation, MemoryDocument.lease_until > now())
    row = await db.scalar(stmt.with_for_update() if lock else stmt)
    if not row:
        raise DocumentError("document_lease_lost")
    return row


async def parse_isolated(data, filename):
    backend = str(Path(__file__).resolve().parents[2])
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "memory.documents.parser", filename,
        cwd=backend, env={"PATH": os.defpath, "PYTHONPATH": backend, "LANG": "C.UTF-8"},
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    async def watch_memory():
        while process.returncode is None:
            if sys.platform == "darwin":
                probe = await asyncio.create_subprocess_exec("/bin/ps", "-o", "rss=", "-p", str(process.pid),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                output, _ = await probe.communicate()
                if output.strip().isdigit() and int(output) > 768 * 1024 and process.returncode is None:
                    process.kill()
                    return
            await asyncio.sleep(0.25)
    monitor = asyncio.create_task(watch_memory())
    try:
        output, _ = await asyncio.wait_for(process.communicate(data), 30)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    finally:
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass
    if process.returncode or len(output) > 4 * 1024 * 1024:
        raise DocumentError("document_parse_limit")
    try:
        value = json.loads(output)
    except ValueError:
        raise DocumentError("document_parse_failed") from None
    if "error" in value:
        raise DocumentError(value["error"])
    return value


async def publish(db, scope, document, revision, config):
    sources, pages = [], []
    locations = revision.metadata_.get("locations", [])
    for index, section in enumerate(revision.sections):
        slug = f"document-{document.id[-24:].lower()}-{index + 1}"
        identity = target_identity(scope, slug, scope.project_id)
        page_id = "wiki_" + identity[:40]
        refs, paragraphs = [], []
        chunk_chars = min(config.source_chunk_chars, 1800)
        for chunk in split_text(section["body"], chunk_chars, overlap=min(180, chunk_chars // 5)):
            source_id = "doc_src_" + canonical_hash([document.id, document.revision, index, chunk["start"]])[:48]
            start, end = section["start"] + chunk["start"], section["start"] + chunk["end"]
            source = MemorySource(id=source_id, source_revision=document.revision, user_id=document.user_id,
                workspace_id=document.workspace_id, project_id=document.project_id, visibility="PERSONAL",
                source_kind="document_chunk", content_hash=text_hash(chunk["text"]), body=chunk["text"],
                source_metadata={"document_id": document.id, "document_hash": revision.content_hash,
                    "filename": document.filename, "page_id": page_id, "section": index,
                    "start": start, "end": end, "edited": section.get("edited", False),
                    "original_pages": [] if section.get("edited") else [location["page"] for location in locations
                        if location["start"] < end and location["end"] > start]},
                status="ACTIVE", acl_epoch=scope.acl_epoch, created_at=now())
            db.add(source)
            sources.append(source)
            refs.append(_source_ref(source, scope))
            paragraphs.append({"text": chunk["text"], "citations": [{"source_id": source.id,
                "revision": source.source_revision, "content_hash": source.content_hash, "quote": chunk["text"]}]})
        body = section["body"] + "\n\n" + " ".join(f"[source:{r['id']}@{r['revision']}]" for r in refs)
        dependency = {"kind": "document", "id": document.id, "revision": document.revision,
            "content_hash": revision.content_hash, "source_ids": [r["id"] for r in refs]}
        page = await db.get(MemoryWikiPage, page_id)
        if page is None:
            page = MemoryWikiPage(id=page_id, target_identity=identity, user_id=document.user_id,
                workspace_id=document.workspace_id, project_id=document.project_id, slug=slug,
                revision=0, created_at=now())
            db.add(page)
        elif page.deleted_at:
            raise DocumentError("document_page_unavailable")
        page.title, page.body, page.content_hash = section["title"], body, text_hash(body)
        page.revision, page.status, page.updated_at = page.revision + 1, "PUBLISHED", now()
        page.source_manifest, page.memory_manifest, page.paragraphs = refs, [dependency], paragraphs
        page.acl_epoch, page.policy_version, page.model = scope.acl_epoch, "document-verbatim-v1", "none"
        page.candidate_id, page.input_hash, page.invalidation_reason = revision.id, revision.content_hash, None
        pages.append(page)
        for ref in [*refs, dependency]:
            db.add(MemoryWikiDependency(id=ascending("wiki_dep"), page_id=page.id, page_revision=page.revision,
                object_kind=ref["kind"], object_id=ref["id"], object_revision=ref["revision"], content_hash=ref["content_hash"]))
    document.source_ids, document.page_ids = [s.id for s in sources], [p.id for p in pages]
    document.status, document.reason_code, document.content_hash = "READY", None, revision.content_hash
    document.lease_until, document.updated_at = None, now()
    await db.flush()
    for source in sources:
        await enqueue_source(db, source, config)
    for page in pages:
        await enqueue_page_outbox(db, page, config)


async def process(lease, config, *, store=None, parser=None):
    async with get_db_session() as db:
        row = await live(db, lease)
        await resolve_access_scope(db, user_id=row.user_id, workspace_id=row.workspace_id, project_id=row.project_id)
        parsed = await db.scalar(select(MemoryDocumentRevision).where(
            MemoryDocumentRevision.document_id == row.id, MemoryDocumentRevision.revision == row.revision))
    if parsed is None:
        if store:
            data = await download_bytes(store, row.storage_key)
        else:
            async with document_storage() as blob:
                data = await asyncio.wait_for(download_bytes(blob, row.storage_key), 30)
        if hashlib.sha256(data).hexdigest() != row.file_hash:
            raise DocumentError("document_file_changed")
        output = await (parser or parse_isolated)(data, row.filename)
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=lease.user_id)
        row = await live(db, lease, lock=True)
        if not config.enabled("wiki", row.user_id):
            raise DocumentError("document_disabled")
        scope = await resolve_access_scope(db, user_id=row.user_id, workspace_id=row.workspace_id, project_id=row.project_id)
        revision = await db.scalar(select(MemoryDocumentRevision).where(
            MemoryDocumentRevision.document_id == row.id, MemoryDocumentRevision.revision == row.revision))
        if revision is None:
            revision = MemoryDocumentRevision(id=ascending("doc_revision"), document_id=row.id, revision=row.revision,
                sections=output["sections"], content_hash=canonical_hash(output["sections"]),
                metadata_=output["metadata"], origin="upload", created_at=now())
            db.add(revision)
        elif canonical_hash(revision.sections) != revision.content_hash:
            raise DocumentError("document_revision_changed")
        await publish(db, scope, row, revision, config)


class MemoryDocumentWorker:
    def __init__(self, config=None, *, store=None, parser=None):
        self.config, self.store, self.parser = config, store, parser
        self.owner = "document:" + uuid.uuid4().hex
        self.task, self.stopping, self.lock = None, asyncio.Event(), asyncio.Lock()

    def start(self):
        self.stopping.clear()
        self.task = asyncio.create_task(self.loop(), name="memory-documents")

    async def stop(self):
        self.stopping.set()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def run_once(self):
        if self.lock.locked():
            return False
        async with self.lock:
            config = self.config or runtime_config.get_config().memory
            lease = await claim(self.owner, config)
            if not lease:
                return False
            try:
                await process(lease, config, store=self.store, parser=self.parser)
            except asyncio.CancelledError:
                await self.fail(lease, config, "document_worker_stopped", retryable=True)
                raise
            except (DocumentError, MemoryAccessDenied) as exc:
                await self.fail(lease, config, str(exc) if isinstance(exc, DocumentError) else "document_scope_unavailable")
            except Exception as exc:
                log.warning("Document processing retry error_type=%s", type(exc).__name__)
                await self.fail(lease, config, "document_processing_failed", retryable=True)
            return True

    async def fail(self, lease, config, code, *, retryable=False):
        async with get_db_session() as db:
            try:
                row = await live(db, lease, lock=True)
            except DocumentError:
                return
            row.status = "RETRY" if retryable and row.attempts < config.max_attempts else "FAILED"
            row.reason_code, row.lease_until, row.updated_at = code, None, now()
            row.available_at = now() + timedelta(seconds=min(60, 2 ** row.attempts))

    async def loop(self):
        while not self.stopping.is_set():
            try:
                if await self.run_once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Document worker recovery error_type=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=(self.config or runtime_config.get_config().memory).worker_interval_seconds)
            except asyncio.TimeoutError:
                pass
