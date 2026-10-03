"""Real SQL + multipart + isolated parsers; uploaded text never becomes a persona."""
from datetime import timedelta
from io import BytesIO
from zipfile import ZipFile

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select, update

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from core import config as runtime_config
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_document import MemoryDocument, MemoryDocumentRevision
from db.models.memory_v2 import MemoryIndexState, MemorySource
from memory.documents import api, service
from memory.documents.parser import DocumentError, parse, split_text
from memory.documents.worker import MemoryDocumentWorker, claim, live, parse_isolated, process
from memory.policy import resolve_access_scope
from memory.retrieval import authorized_documents, search_memory
from memory.wiki import editing, reader
from memory.wiki.service import WikiStateError, now
from tests.unit.test_memory_wiki import seed, wiki_database  # noqa: F401


class Blob:
    def __init__(self):
        self.files = {}
    async def upload(self, key, data):
        self.files[key] = data
    async def download(self, key):
        return self.files[key]


async def ingest(data, body, *, filename="运营手册.md", store=None):
    store = store or Blob()
    doc = await service.submit(user_id=data[0], workspace_id=data[1], project_id=data[2],
        filename=filename, data=body.encode() if isinstance(body, str) else body, store=store)
    assert await MemoryDocumentWorker(data[4], store=store).run_once()
    async with get_db_session() as db:
        return await db.get(MemoryDocument, doc["id"]), store


@pytest.mark.asyncio
async def test_upload_chunks_wiki_full_coverage_and_duplicate_file(monkeypatch):
    data = await seed(monkeypatch)
    text = "# 北岸展馆运营手册\n\n" + ("开放日每天 09:30 开馆，周一闭馆。夜场入场需提前预约。\n\n" * 600) + "退款最迟在活动前 48 小时办理。"
    doc, store = await ingest(data, text)
    assert doc.status == "READY", doc.reason_code
    assert len(doc.source_ids) > len(doc.page_ids) > 1
    duplicate = await service.submit(user_id=data[0], workspace_id=data[1], project_id=data[2],
        filename="另一文件名.md", data=text.encode(), store=store)
    assert duplicate["id"] == doc.id
    async with get_db_session() as db:
        revision = await db.scalar(select(MemoryDocumentRevision).where(MemoryDocumentRevision.document_id == doc.id))
        assert "".join(s["body"] for s in revision.sections) == text
        assert len((await db.scalars(select(UserMemory).where(UserMemory.user_id == data[0]))).all()) == 1
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        docs = await authorized_documents(db, scope, data[4])
        chunks = [d for d in docs if d.kind == "source" and d.category == "DOCUMENT"]
        assert len(chunks) == len(doc.source_ids)
        assert any("48 小时" in d.text for d in chunks)
        assert all(d.confirmation_status == "UPLOADED_DOCUMENT" for d in chunks)
    pages = await reader.library(user_id=data[0], workspace_id=data[1])
    assert len(pages["pages"]) == 1  # One document entry, not a card per chunk/section.
    detail = await reader.page_detail(user_id=data[0], workspace_id=data[1], page_id=doc.page_ids[0])
    assert detail["body_available"] and len(detail["document"]["sections"]) == len(doc.page_ids)
    assert detail["source_details"][0]["filename"] == "运营手册.md"


@pytest.mark.asyncio
async def test_edits_invalidate_old_chunks_immediately_and_keep_history(monkeypatch):
    data = await seed(monkeypatch)
    doc, store = await ingest(data, "# 场馆规则\n\n周一闭馆，周二可以入场。")
    snapshot = await editing.snapshot(user_id=data[0], workspace_id=data[1], page_id=doc.page_ids[0])
    result = await editing.save(user_id=data[0], workspace_id=data[1], page_id=doc.page_ids[0],
        expected_revision=snapshot["revision"], content_hash=snapshot["content_hash"], title="场馆规则",
        entries=[{**snapshot["entries"][0], "text": "# 场馆规则\n\n周一开放，周二闭馆。"}], request_id="edit-document")
    assert result["status"] == "updating"
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        assert not await authorized_documents(db, scope, data[4], only={("source", i) for i in doc.source_ids})
    assert not (await reader.page_detail(user_id=data[0], workspace_id=data[1], page_id=doc.page_ids[0]))["body_available"]
    assert await MemoryDocumentWorker(data[4], store=store).run_once()
    detail = await reader.page_detail(user_id=data[0], workspace_id=data[1], page_id=doc.page_ids[0])
    assert "周一开放" in detail["body"] and "周一闭馆" not in detail["body"]
    assert detail["source_details"][0]["edited"] and not detail["source_details"][0]["original_pages"]
    async with get_db_session() as db:
        assert len((await db.scalars(select(MemoryDocumentRevision).where(MemoryDocumentRevision.document_id == doc.id))).all()) == 2
        assert (await db.get(MemorySource, doc.source_ids[0])).body == "# 场馆规则\n\n周一闭馆，周二可以入场。"
    with pytest.raises(WikiStateError, match="wiki_edit_changed"):
        await editing.save(user_id=data[0], workspace_id=data[1], page_id=doc.page_ids[0],
            expected_revision=snapshot["revision"], content_hash=snapshot["content_hash"], title="stale",
            entries=snapshot["entries"], request_id="late-save")


@pytest.mark.asyncio
async def test_tenant_isolation_before_retrieval_and_http_download(monkeypatch, tmp_path):
    data = await seed(monkeypatch)
    full = runtime_config.get_config()
    full.blob_provider, full.blob_local_path = "local", str(tmp_path / "blobs")
    app = FastAPI()
    app.include_router(api.router)
    actor = {"user_id": data[0], "workspace_id": data[1]}
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_workspace] = lambda: actor["workspace_id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://documents.test") as client:
        response = await client.post("/api/memory-documents", data={"project_id": data[2]},
            files={"file": ("私有资料.txt", "蓝石厅只接受提前预约。".encode(), "text/plain")})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        doc_id = response.json()["id"]
        assert await MemoryDocumentWorker(data[4]).run_once()
        original = await client.get(f"/api/memory-documents/{doc_id}/original")
        assert original.status_code == 200 and original.content.decode() == "蓝石厅只接受提前预约。"
        bad = await client.post("/api/memory-documents", files={"file": ("run.exe", b"MZ")})
        assert bad.status_code == 422
        other = await seed(monkeypatch)
        actor.update(user_id=other[0], workspace_id=other[1])
        assert (await client.get(f"/api/memory-documents/{doc_id}/original")).status_code == 404
        assert (await client.post(f"/api/memory-documents/{doc_id}/retry")).status_code == 404
        assert (await client.get("/api/memory-documents")).json()["documents"] == []
        async with get_db_session() as db:
            scope = await resolve_access_scope(db, user_id=other[0], workspace_id=other[1])
            assert not [d for d in await authorized_documents(db, scope, other[4]) if d.category == "DOCUMENT"]


@pytest.mark.asyncio
async def test_expired_worker_cannot_publish_after_reclaim(monkeypatch):
    data = await seed(monkeypatch)
    blob = Blob()
    doc = await service.submit(user_id=data[0], workspace_id=data[1], project_id=data[2],
        filename="retry.txt", data=b"The archive opens on Tuesdays.", store=blob)
    old = await claim("old", data[4])
    async with get_db_session() as db:
        await db.execute(update(MemoryDocument).where(MemoryDocument.id == doc["id"]).values(lease_until=now() - timedelta(seconds=1)))
    new = await claim("new", data[4])
    assert new.generation == old.generation + 1
    with pytest.raises(DocumentError, match="document_lease_lost"):
        await process(old, data[4], store=blob)
    await process(new, data[4], store=blob)
    async with get_db_session() as db:
        assert (await db.get(MemoryDocument, doc["id"])).status == "READY"


@pytest.mark.asyncio
async def test_corrupt_file_is_failed_not_silently_published(monkeypatch):
    data = await seed(monkeypatch)
    doc, _ = await ingest(data, b"not-a-PDF", filename="broken.pdf")
    assert doc.status == "FAILED" and doc.reason_code == "document_parse_failed"
    assert not doc.source_ids and not doc.page_ids


def docx_bytes():
    output = BytesIO()
    with ZipFile(output, "w") as z:
        z.writestr("word/document.xml", '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
          <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>入场规定</w:t></w:r></w:p>
          <w:p><w:r><w:t>周三休馆，门票不可转让。</w:t></w:r></w:p>
          <w:tbl><w:tr><w:tc><w:p><w:r><w:t>类型</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>人数</w:t></w:r></w:p></w:tc></w:tr>
          <w:tr><w:tc><w:p><w:r><w:t>团体</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>12</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>''')
    return output.getvalue()


def pdf_bytes():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 50 700 Td (The Blue Hall opens at 09:30. No entry on Mondays.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,data,expected", [
    ("rules.docx", docx_bytes(), ["# 入场规定", "周三休馆", "| 团体 | 12 |"]),
    ("rules.pdf", pdf_bytes(), ["09:30", "No entry on Mondays."]),
    ("rules.csv", "名称,人数\n蓝石厅,12\n".encode(), ["| 蓝石厅 | 12 |"]),
    ("rules.html", b"<h1>Rules</h1><script>EVIL()</script><p>No entry on Mondays.</p>", ["# Rules", "No entry on Mondays."]),
])
async def test_real_isolated_format_parsers(filename, data, expected):
    value = await parse_isolated(data, filename)
    text = "".join(s["body"] for s in value["sections"])
    assert all(e in text for e in expected) and "EVIL" not in text
    if filename.endswith(".pdf"):
        assert value["metadata"]["locations"][0]["page"] == 1


def test_chunks_cover_every_character_with_bounded_overlap():
    body = "一段很长但不可丢失的规则。\n\n" * 500
    chunks = list(split_text(body, 1800, overlap=180))
    assert chunks[0]["start"] == 0 and chunks[-1]["end"] == len(body)
    assert all(c["text"] == body[c["start"]:c["end"]] and len(c["text"]) <= 1800 for c in chunks)
    assert all(b["start"] == a["end"] - 180 for a, b in zip(chunks, chunks[1:]))


@pytest.mark.asyncio
async def test_outbox_indexes_document_chunk_and_dense_search_rechecks_current_revision(monkeypatch):
    from uuid import uuid4
    from db.models.memory_v2 import MemoryOutbox
    from memory.outbox import OutboxLease, deliver_outbox
    from memory.index.base import IndexHit
    from tests.unit.test_memory_retrieval_runtime import FakeEmbedding, FakeIndex
    data = await seed(monkeypatch)
    config = data[4]
    config.index_sync, config.retrieval_v2 = True, True
    config.index_generation = "doc-test-" + uuid4().hex[:20]
    doc, _ = await ingest(data, "# 蓝石厅\n团体入场人数上限是 12 人，休息日不开放。")
    source_id = doc.source_ids[0]
    async with get_db_session() as db:
        row = await db.scalar(select(MemoryOutbox).where(MemoryOutbox.object_id == source_id, MemoryOutbox.operation == "UPSERT"))
        row.status, row.lease_owner, row.lease_generation, row.lease_until = "RUNNING", "test", 1, now() + timedelta(seconds=60)
        lease = OutboxLease(row.id, "test", 1, "source", source_id, 1, "UPSERT", config.index_generation, data[0], data[1], data[2])
    index, embedding = FakeIndex(config), FakeEmbedding()
    assert await deliver_outbox(lease, config, index=index, embedding=embedding)
    assert any(key[1] == source_id for key in index.points)
    index.hits = [IndexHit("source", source_id, 1, .95)]
    result = await search_memory(query="团队能预约多少个名额？", user_id=data[0], workspace_id=data[1], project_id=data[2],
        config=config, index=index, embedding=embedding)
    assert any(i["id"] == source_id and "12 人" in i["text"] and i["dense_score"] is not None for i in result["items"])
    async with get_db_session() as db:
        assert (await service.document_view(db, await db.get(MemoryDocument, doc.id), config))["status"] == "ready"
        from memory.service import read_source_in_scope
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        evidence = await read_source_in_scope(db, access=scope, source_id=source_id, source_revision=1)
        assert evidence["available"] and "12 人" in evidence["body"]
        state = await db.scalar(select(MemoryIndexState).where(MemoryIndexState.object_id == source_id))
        state.status, state.chunk_ids = "INELIGIBLE", []
    index.points.clear()
    from memory import reconcile
    monkeypatch.setattr(reconcile, "QdrantMemoryIndex", lambda *args, **kwargs: index)
    await reconcile.reconcile(scope, config)
    async with get_db_session() as db:
        repair = await db.scalar(select(MemoryOutbox).where(MemoryOutbox.object_id == source_id,
            MemoryOutbox.event_id.startswith("reconcile:"), MemoryOutbox.operation == "UPSERT"))
        assert repair is not None  # Empty/ineligible is not a healthy index manifest.
        row = await db.get(MemoryDocument, doc.id)
        row.revision += 1
        row.status = "PENDING"
    result = await search_memory(query="团队能预约多少个名额？", user_id=data[0], workspace_id=data[1], project_id=data[2],
        config=config, index=index, embedding=embedding)
    assert not any(i["id"] == source_id for i in result["items"])


class DeletingBlob(Blob):
    async def delete(self, key):
        self.files.pop(key, None)


@pytest.mark.asyncio
async def test_owner_deletes_a_file_and_everything_built_from_it(monkeypatch):
    from db.models.memory_v2 import MemoryOutbox
    data = await seed(monkeypatch)
    store = DeletingBlob()
    doc, _ = await ingest(data, "# 场馆规则\n\n周一闭馆，周二可以入场。", store=store)
    keep, _ = await ingest(data, "# 茶歇安排\n\n每周三下午三点茶歇。", filename="茶歇.md", store=store)
    assert await service.delete(user_id=data[0], workspace_id=data[1], document_id="memory_doc_missing", store=store) is None
    result = await service.delete(user_id=data[0], workspace_id=data[1], document_id=doc.id, store=store)
    assert result == {"ok": True, "status": "deleted", "original_cleanup": "done"}
    assert doc.storage_key not in store.files and keep.storage_key in store.files
    async with get_db_session() as db:
        assert await db.get(MemoryDocument, doc.id) is None
        assert not (await db.scalars(select(MemoryDocumentRevision).where(
            MemoryDocumentRevision.document_id == doc.id))).all()
        sources = (await db.scalars(select(MemorySource).where(MemorySource.id.in_(doc.source_ids)))).all()
        assert sources and all(s.status == "DELETED" and s.body is None and s.source_metadata == {} for s in sources)
        withdrawn = (await db.scalars(select(MemoryOutbox).where(MemoryOutbox.object_id.in_(doc.source_ids),
            MemoryOutbox.operation == "DELETE"))).all()
        assert {row.object_id for row in withdrawn} == set(doc.source_ids)
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        texts = [d.text for d in await authorized_documents(db, scope, data[4])]
        assert not any("周一闭馆" in text for text in texts) and any("茶歇" in text for text in texts)
    library = await reader.library(user_id=data[0], workspace_id=data[1])
    assert [page["id"] for page in library["pages"]] == keep.page_ids[:1]
    assert await reader.page_detail(user_id=data[0], workspace_id=data[1], page_id=doc.page_ids[0]) is None
    found = await search_memory(query="周一闭馆", user_id=data[0], workspace_id=data[1], project_id=data[2], config=data[4])
    assert not any("周一闭馆" in item["text"] for item in found["items"])
    # Deleting is not a ban: the same file can be added again later.
    again = await service.submit(user_id=data[0], workspace_id=data[1], project_id=data[2],
        filename="场馆规则.md", data="# 场馆规则\n\n周一闭馆，周二可以入场。".encode(), store=store)
    assert again["id"] != doc.id and again["status"] == "pending"


@pytest.mark.asyncio
async def test_delete_route_is_owner_scoped(monkeypatch, tmp_path):
    data = await seed(monkeypatch)
    full = runtime_config.get_config()
    full.blob_provider, full.blob_local_path = "local", str(tmp_path / "blobs")
    app = FastAPI()
    app.include_router(api.router)
    actor = {"user_id": data[0], "workspace_id": data[1]}
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_workspace] = lambda: actor["workspace_id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://documents.test") as client:
        response = await client.post("/api/memory-documents", data={"project_id": data[2]},
            files={"file": ("私有资料.txt", "蓝石厅只接受提前预约。".encode(), "text/plain")})
        doc_id = response.json()["id"]
        assert await MemoryDocumentWorker(data[4]).run_once()
        other = await seed(monkeypatch)
        actor.update(user_id=other[0], workspace_id=other[1])
        assert (await client.delete(f"/api/memory-documents/{doc_id}")).status_code == 404
        actor.update(user_id=data[0], workspace_id=data[1])
        deleted = await client.delete(f"/api/memory-documents/{doc_id}")
        assert deleted.status_code == 200 and deleted.json()["status"] == "deleted"
        assert (await client.get(f"/api/memory-documents/{doc_id}/original")).status_code == 404
        assert (await client.get("/api/memory-documents")).json()["documents"] == []
        assert (await client.delete(f"/api/memory-documents/{doc_id}")).status_code == 404


async def edited_twice(data, store):
    doc, _ = await ingest(data, "# 场馆规则\n\n周一闭馆，周二可以入场。", store=store)
    first = list(doc.source_ids)
    snapshot = await editing.snapshot(user_id=data[0], workspace_id=data[1], page_id=doc.page_ids[0])
    await editing.save(user_id=data[0], workspace_id=data[1], page_id=doc.page_ids[0],
        expected_revision=snapshot["revision"], content_hash=snapshot["content_hash"], title="场馆规则",
        entries=[{**snapshot["entries"][0], "text": "# 场馆规则\n\n周一开放，周二闭馆。"}], request_id="edit-twice")
    assert await MemoryDocumentWorker(data[4], store=store).run_once()
    async with get_db_session() as db:
        doc = await db.get(MemoryDocument, doc.id)
    assert set(first).isdisjoint(doc.source_ids)
    return doc, first


@pytest.mark.asyncio
async def test_deleting_an_edited_file_clears_the_text_of_every_revision(monkeypatch):
    data = await seed(monkeypatch)
    store = DeletingBlob()
    doc, first = await edited_twice(data, store)
    other, _ = await ingest(data, "# 茶歇安排\n\n每周三下午三点茶歇。", filename="茶歇.md", store=store)
    assert (await service.delete(user_id=data[0], workspace_id=data[1], document_id=doc.id, store=store))["ok"]
    async with get_db_session() as db:
        cleared = (await db.scalars(select(MemorySource).where(MemorySource.id.in_([*first, *doc.source_ids])))).all()
        assert len(cleared) == len(first) + len(doc.source_ids)
        assert all(s.status == "DELETED" and s.body is None and s.source_metadata == {} for s in cleared)
        kept = (await db.scalars(select(MemorySource).where(MemorySource.id.in_(other.source_ids)))).all()
        assert kept and all(s.status == "ACTIVE" and s.body for s in kept)


class FailingBlob(DeletingBlob):
    def __init__(self):
        super().__init__()
        self.broken = True

    def recover(self):
        self.broken = False

    async def delete(self, key):
        if self.broken:
            raise TimeoutError("storage unavailable")
        await super().delete(key)


class SilentBlob(FailingBlob):
    """Like storage clients that swallow delete errors and report nothing."""
    async def delete(self, key):
        if not self.broken:
            await DeletingBlob.delete(self, key)

    async def exists(self, key):
        return key in self.files


async def due_now():
    from db.models.memory_document import MemoryDocumentCleanup
    async with get_db_session() as db:
        for row in (await db.scalars(select(MemoryDocumentCleanup))).all():
            row.available_at = now() - timedelta(seconds=1)


async def pending_cleanups(data):
    return (await service.list_documents(user_id=data[0], workspace_id=data[1]))["cleanup_pending"]


@pytest.mark.asyncio
@pytest.mark.parametrize("store_kind", [FailingBlob, SilentBlob])
async def test_a_failed_original_removal_is_retried_until_the_file_is_gone(monkeypatch, store_kind):
    data = await seed(monkeypatch)
    store = store_kind()
    doc, _ = await ingest(data, "# 场馆规则\n\n周一闭馆。", store=store)
    result = await service.delete(user_id=data[0], workspace_id=data[1], document_id=doc.id, store=store)
    assert result == {"ok": True, "status": "deleted", "original_cleanup": "pending"}
    assert doc.storage_key in store.files and await pending_cleanups(data) == 1
    store.recover()
    await due_now()
    assert await service.retry_original_cleanups(store=store) == 1
    assert doc.storage_key not in store.files and await pending_cleanups(data) == 0


@pytest.mark.asyncio
async def test_a_removal_interrupted_after_the_delete_commits_resumes(monkeypatch):
    data = await seed(monkeypatch)
    store = DeletingBlob()
    doc, _ = await ingest(data, "# 场馆规则\n\n周一闭馆。", store=store)

    async def process_exited(cleanup_id, *, store=None):
        return False

    remove_original = service.remove_original
    monkeypatch.setattr(service, "remove_original", process_exited)
    assert (await service.delete(user_id=data[0], workspace_id=data[1], document_id=doc.id,
                                 store=store))["original_cleanup"] == "pending"
    monkeypatch.setattr(service, "remove_original", remove_original)
    await due_now()
    assert await service.retry_original_cleanups(store=store) == 1
    assert doc.storage_key not in store.files


@pytest.mark.asyncio
async def test_a_late_removal_never_deletes_the_same_file_uploaded_again(monkeypatch):
    data = await seed(monkeypatch)
    store = FailingBlob()
    text = "# 场馆规则\n\n周一闭馆。"
    old, _ = await ingest(data, text, store=store)
    assert (await service.delete(user_id=data[0], workspace_id=data[1], document_id=old.id,
                                 store=store))["original_cleanup"] == "pending"
    new, _ = await ingest(data, text, store=store)
    assert new.id != old.id and new.storage_key != old.storage_key
    store.recover()
    await due_now()
    assert await service.retry_original_cleanups(store=store) == 1
    assert old.storage_key not in store.files and store.files[new.storage_key] == text.encode()


@pytest.mark.asyncio
async def test_a_failing_cleanup_pass_never_holds_up_document_processing(monkeypatch):
    data = await seed(monkeypatch)

    async def broken(**kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(service, "retry_original_cleanups", broken)
    worker = MemoryDocumentWorker(data[4], store=Blob())
    await worker.cleanup_originals()  # logged, not raised
    doc, _ = await ingest(data, "# 场馆规则\n\n周一闭馆。")
    assert doc.status == "READY"
