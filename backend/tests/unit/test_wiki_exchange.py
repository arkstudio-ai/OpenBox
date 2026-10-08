"""Independent upstream-layout fixtures, format round trips and admission fences."""
import io
import json
from pathlib import Path
import zipfile

import pytest

from db.base import get_db_session
from memory.policy import resolve_access_scope
from memory.wiki import exchange, export, service
from tests.unit.test_memory_wiki import seed, wiki_database  # noqa: F401
from wiki_compiler.exchange import ExchangeError, parse_okf, zip_files

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/wiki_okf"


def bundle():
    return zip_files({str(path.relative_to(FIXTURE)): path.read_text() for path in FIXTURE.rglob("*") if path.is_file()})


def decide_args(data, document):
    return dict(user_id=data[0], workspace_id=data[1], document_id=document["id"],
                expected_revision=document["revision"], content_hash=document["content_hash"])


@pytest.mark.asyncio
async def test_foreign_bundle_review_roundtrip_and_every_export_format(monkeypatch):
    data = await seed(monkeypatch)
    args = dict(user_id=data[0], workspace_id=data[1], project_id=data[2])
    preview = await exchange.preview(**args, data=bundle())
    assert not preview["foreign_history_executable"] and preview["warnings"] == []
    assert preview["id"] == (await exchange.preview(**args, data=bundle()))["id"]
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, **args)
        assert await service.authorized_wiki_documents(db, scope, data[4]) == []
    document = preview["documents"][0]
    published = await exchange.decide(**decide_args(data, document), action="approve")
    assert published["status"] == "approved"
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, **args)
        assert len(await service.authorized_wiki_documents(db, scope, data[4])) == 1
    archive, media, filename = await export.export(**args)
    parsed = parse_okf(archive)
    reexported = parsed["documents"][0]
    assert reexported["path"] == "concepts/release.md"
    assert reexported["frontmatter"]["type"] == "policy"
    assert reexported["frontmatter"]["x-vendor-example"]["nested"]["preserve"] == "this"
    assert parsed["manifest"]["x-vendor-example"]["preserved"] is True
    assert parsed["manifest"]["x-llmwiki"]["workflows"][0]["runId"] == "foreign-approved-run"
    assert "references/team-notes-2d372104.md" in parsed["attachments"]
    assert parsed["warnings"] == [] and media == "application/zip" and filename.endswith(".zip")
    from memory.wiki.reader import page_detail
    reader = await page_detail(user_id=data[0], workspace_id=data[1], page_id=published["page_id"])
    assert reader["exchange_path"] == "concepts/release.md"
    assert reader["exchange_links"]["references/team-notes-2d372104.md"]["source_id"]
    for format in ["json", "jsonld", "graphml", "marp", "llms"]:
        content, _, _ = await export.export(**args, format=format)
        assert b"Release policy" in content


@pytest.mark.asyncio
async def test_foreign_assets_and_unknown_producer_fields_survive_reviewed_roundtrip(monkeypatch):
    from wiki_compiler.exchange import frontmatter, render_markdown
    data = await seed(monkeypatch)
    args = dict(user_id=data[0], workspace_id=data[1], project_id=data[2])
    files = {str(p.relative_to(FIXTURE)): p.read_text() for p in FIXTURE.rglob("*") if p.is_file()}
    meta, body = frontmatter(files["index.md"])
    meta["x-openbox"] = {"futureField": {"keep": True}, "profiles": [{"id": "foreign-only"}]}
    files["index.md"] = render_markdown(meta, body)
    files["assets/notes.json"] = '{"method": "independent-reference"}'
    preview = await exchange.preview(**args, data=zip_files(files))
    await exchange.decide(**decide_args(data, preview["documents"][0]), action="approve")
    parsed = parse_okf((await export.export(**args))[0])
    assert parsed["attachments"]["assets/notes.json"] == files["assets/notes.json"]
    assert parsed["manifest"]["x-openbox"]["futureField"] == {"keep": True}
    original = parsed["manifest"]["x-openbox-foreign-bundles"][preview["id"]]["manifest"]
    assert original["x-openbox"]["profiles"] == [{"id": "foreign-only"}]
    assert (await export.snapshot(**args))["profiles"] == []


def test_native_links_resolve_only_same_project_and_preserve_code():
    from wiki_compiler.export import native_links, okf_files
    page = {"id": "one", "slug": "one", "title": "One", "project_id": "p"}
    target = {"id": "two", "slug": "two", "title": "Two", "project_id": "p"}
    foreign = {"id": "other", "slug": "two", "title": "Two", "project_id": "other"}
    body = 'See [[two|second]]. `[[two]]`\n```\n[[two]]\n```\n\\[[two]]'
    rewritten = native_links(body, page, [page, target, foreign], {"one": "concepts/one.md", "two": "concepts/two.md"})
    assert rewritten == 'See [second](/concepts/two.md). `[[two]]`\n```\n[[two]]\n```\n\\[[two]]'
    # Upstream treats every typed Markdown file as a knowledge page. A source
    # export must stay raw, while its revision/hash belongs to page metadata.
    source = {"id": "s1", "revision": 3, "content_hash": "a" * 64, "body": "Original source evidence.\n"}
    snapshot = {"pages": [{**page, "body": "Grounded knowledge.", "sources": [{"id": "s1"}],
        "revision": 2, "updated_at": "2026-10-02T00:00:00Z"}], "sources": {"s1": source},
        "foreign_bundles": {}, "snapshot_hash": "snapshot", "concepts": [], "relations": [], "profiles": [], "workflows": []}
    files = okf_files(snapshot)
    assert files["references/s1.md"] == source["body"]
    parsed = parse_okf(zip_files(files))
    assert len(parsed["documents"]) == 1
    assert parsed["documents"][0]["frontmatter"]["x-openbox"]["sourceVersions"] == [
        {"id": "s1", "file": "references/s1.md", "revision": 3, "contentHash": "a" * 64}]


def test_fixture_generated_by_unmodified_upstream_renderer_is_accepted():
    root = FIXTURE.parent / "wiki_okf_upstream"
    files = {str(p.relative_to(root)): p.read_text() for p in root.rglob("*") if p.is_file()}
    parsed = parse_okf(zip_files(files))
    assert {doc["title"] for doc in parsed["documents"]} == {"Decision record guide", "Release note checklist"}
    assert parsed["warnings"] == [{"path": "concepts/release-note-checklist.md", "code": "producer_hash_mismatch"}]
    assert "references/team-guide-03840965.md" in parsed["attachments"]


@pytest.mark.asyncio
async def test_conflicts_require_explicit_rename_and_never_overwrite(monkeypatch):
    data = await seed(monkeypatch)
    args = dict(user_id=data[0], workspace_id=data[1], project_id=data[2])
    one = (await exchange.preview(**args, data=bundle()))["documents"][0]
    await exchange.decide(**decide_args(data, one), action="approve")
    files = {str(path.relative_to(FIXTURE)): path.read_text() for path in FIXTURE.rglob("*") if path.is_file()}
    files["concepts/release.md"] += "\nAn additional note.\n"
    other = (await exchange.preview(**args, data=zip_files(files)))["documents"][0]
    assert other["conflict"]
    with pytest.raises(service.WikiStateError, match="target_conflict"):
        await exchange.decide(**decide_args(data, other), action="approve")
    renamed = await exchange.decide(**decide_args(data, other), action="rename", slug="release-policy-v2")
    assert not renamed["conflict"]
    await exchange.decide(**decide_args(data, renamed), action="approve")
    snapshot = await export.snapshot(**args)
    assert len(snapshot["pages"]) == 2
    other_user = await seed(monkeypatch)
    assert await exchange.detail(user_id=other_user[0], workspace_id=other_user[1], bundle_id=one.get("bundle_id", "missing")) is None


@pytest.mark.asyncio
async def test_changed_import_reference_hides_page_and_export(monkeypatch):
    from db.models.memory_v2 import MemorySource
    from sqlalchemy import select
    data = await seed(monkeypatch)
    args = dict(user_id=data[0], workspace_id=data[1], project_id=data[2])
    preview = await exchange.preview(**args, data=bundle())
    await exchange.decide(**decide_args(data, preview["documents"][0]), action="approve")
    async with get_db_session() as db:
        source = await db.scalar(select(MemorySource).where(MemorySource.user_id == data[0], MemorySource.id.like("wiki_ref_%")))
        source.body = "changed without updating the original hash"
    assert (await export.snapshot(**args))["pages"] == []
    detail = await exchange.detail(user_id=data[0], workspace_id=data[1], bundle_id=preview["id"])
    assert detail["documents"][0]["body"] is None


@pytest.mark.parametrize("path", ["../escape.md", "/absolute.md", "a/../../escape.md", "C:/escape.md", "a\\escape.md"])
def test_archive_paths_rejected(path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(path, "bad")
    with pytest.raises(ExchangeError, match="unsafe_path"):
        parse_okf(stream.getvalue())


@pytest.mark.parametrize("yaml", ["okf_version: '9.0'", "okf_version: '0.1'\nx: &a [*a]", "okf_version: '0.1'\nokf_version: '0.1'"])
def test_versions_aliases_and_duplicate_keys_rejected(yaml):
    with pytest.raises(ExchangeError):
        parse_okf(zip_files({"index.md": "---\n" + yaml + "\n---\n"}))


def test_symlink_duplicate_archive_entries_and_compression_bomb_rejected():
    for kind in ["symlink", "duplicate", "bomb"]:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if kind == "symlink":
                entry = zipfile.ZipInfo("index.md")
                entry.external_attr = 0o120777 << 16
                archive.writestr(entry, "../secret")
            elif kind == "duplicate":
                archive.writestr("index.md", "one")
                archive.writestr("INDEX.md", "two")
            else:
                archive.writestr("index.md", "a" * 200000)
        with pytest.raises(ExchangeError):
            parse_okf(stream.getvalue())
