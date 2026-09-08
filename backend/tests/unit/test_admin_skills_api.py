"""Store moderation: admin-only, audited, announced once, and never leaky."""
import io
import uuid
import zipfile
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy
from fastapi import FastAPI
from sqlalchemy import delete, func, select

from api import admin_skills
from api.admin_skills import MAX_LISTED_FILES, router
from auth.middleware import get_current_user
from db.base import get_db_session, get_engine
from db.models.audit_log import AuditLog
from db.models.catalog_override import CatalogOverride
from db.models.notification import Notification
from db.models.user import User
from db.repository.user_repo import PgUserRepo
from skill import user_library


SKILL_MD = """---
name: greeter
description: Return one approved greeting.
---

# Greeter

Say hello, once, and stop.
"""

#: Every route, with a body where the endpoint needs one. Used to prove the
#: router's admin dependency covers all of them rather than most of them.
ROUTES = (
    ("GET", "/api/admin/skills/store", None),
    ("POST", "/api/admin/skills/store/community:x/listing", {"listing": "listed"}),
    ("POST", "/api/admin/skills/store/community:x/featured", {"featured": True}),
    ("POST", "/api/admin/skills/store/community:x/official", {"is_official": True}),
    ("GET", "/api/admin/skills/review", None),
    ("GET", "/api/admin/skills/review/community:x", None),
    ("GET", "/api/admin/skills/review/community:x/archive", None),
    ("POST", "/api/admin/skills/review/community:x/approve", None),
    ("POST", "/api/admin/skills/review/community:x/reject", {"note": "no"}),
    ("GET", "/api/admin/skills/installs", None),
)


def _client(identity: dict) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: dict(identity)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _zip(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in members.items():
            archive.writestr(path, content)
    return buffer.getvalue()


async def _audits(action: str, catalog_id: str) -> int:
    async with get_db_session() as session:
        return await session.scalar(
            select(func.count()).select_from(AuditLog)
            .where(AuditLog.action == action, AuditLog.resource_id == catalog_id)
        )


async def _notices(user_id: str, kind: str) -> list[Notification]:
    async with get_db_session() as session:
        return list((await session.execute(
            select(Notification)
            .where(Notification.user_id == user_id, Notification.kind == kind)
        )).scalars())


async def _submit(author_id: str, slug: str, archive: bytes) -> str:
    """Publish one package into the review queue, as review-on deployments do."""
    created = await user_library.upsert_personal_snapshot(
        author_id,
        {"name": slug, "install_dir": slug, "description": "Greet once.",
         "icon": "👋", "files": []},
        archive,
    )
    published = await user_library.publish_personal_skill(
        author_id, created["id"], review_required=True
    )
    assert published["listing"] == user_library.LISTING_PENDING
    return f"community:{published['id']}"


@pytest.fixture
async def clean_overrides():
    """Catalogue shelf decisions are global rows; do not leak them between tests."""
    yield
    async with get_db_session() as session:
        await session.execute(delete(CatalogOverride))


@pytest.fixture
async def store():
    """An operator, an author, and one submission waiting for a verdict."""
    suffix = uuid.uuid4().hex[:10]
    repo = PgUserRepo()
    admin = await repo.create(
        id=f"askadm-{suffix}", username=f"askadm-{suffix}",
        password_hash="unused", role="admin",
    )
    author = await repo.create(
        id=f"askaut-{suffix}", username=f"askaut-{suffix}",
        password_hash="unused", email=f"author.{suffix}@skills.test",
    )
    slug = f"greeter-{suffix}"
    archive = _zip({f"{slug}/SKILL.md": SKILL_MD, f"{slug}/reference.md": "notes"})
    return SimpleNamespace(
        suffix=suffix,
        admin={"user_id": admin["id"], "role": "admin"},
        author=author,
        slug=slug,
        archive=archive,
        catalog_id=await _submit(author["id"], slug, archive),
    )


async def test_every_route_refuses_a_non_admin(store):
    async with _client({"user_id": store.author["id"], "role": "user"}) as client:
        for method, path, body in ROUTES:
            response = await client.request(method, path, json=body)
            assert response.status_code == 403, f"{method} {path}"


async def test_store_merges_both_sources_filters_and_pages(store):
    async with _client(store.admin) as client:
        everything = await client.get("/api/admin/skills/store", params={"limit": 200})
        third_party = await client.get(
            "/api/admin/skills/store", params={"origin": "third_party", "limit": 200}
        )
        mcp_only = await client.get(
            "/api/admin/skills/store", params={"kind": "mcp", "limit": 200}
        )
        pending = await client.get(
            "/api/admin/skills/store", params={"listing": "pending", "limit": 200}
        )
        searched = await client.get(
            "/api/admin/skills/store", params={"q": store.slug}
        )
        first = await client.get(
            "/api/admin/skills/store", params={"sort": "name", "limit": 1}
        )
        second = await client.get(
            "/api/admin/skills/store", params={"sort": "name", "limit": 1, "offset": 1}
        )
        bad_sort = await client.get("/api/admin/skills/store", params={"sort": "price"})

    assert everything.status_code == 200
    items = everything.json()["items"]
    keys = {item["catalog_id"] for item in items}
    assert store.catalog_id in keys
    assert {"skill:web-research", "mcp:playwright"} <= keys
    # A store list is metadata; the archives behind it stay in the database.
    assert "archive_data" not in everything.text
    assert all("install" not in item and "config" not in item for item in items)

    assert {item["source"] for item in third_party.json()["items"]} == {"catalog"}
    assert all(item["origin"] == "third_party" for item in third_party.json()["items"])
    # Only submissions have an author, and only skills can be submitted.
    assert all(item["kind"] == "mcp" for item in mcp_only.json()["items"])
    assert store.catalog_id not in {i["catalog_id"] for i in mcp_only.json()["items"]}

    assert all(item["listing"] == "pending" for item in pending.json()["items"])
    assert store.catalog_id in {i["catalog_id"] for i in pending.json()["items"]}
    assert [item["catalog_id"] for item in searched.json()["items"]] == [
        store.catalog_id
    ]
    submission = searched.json()["items"][0]
    assert submission["author"]["email"] == store.author["email"]
    assert submission["installs_count"] == 0
    assert submission["sha256"]

    assert first.json()["total"] == second.json()["total"] > 1
    assert len(first.json()["items"]) == len(second.json()["items"]) == 1
    assert first.json()["items"][0] != second.json()["items"][0]
    assert bad_sort.status_code == 422


async def test_taking_something_down_always_states_a_reason(store):
    async with _client(store.admin) as client:
        blank = await client.post(
            f"/api/admin/skills/store/{store.catalog_id}/listing",
            json={"listing": "delisted"},
        )
        whitespace = await client.post(
            f"/api/admin/skills/store/{store.catalog_id}/listing",
            json={"listing": "delisted", "note": "   "},
        )
        rejected = await client.post(
            f"/api/admin/skills/review/{store.catalog_id}/reject", json={"note": ""}
        )
        missing = await client.post(
            f"/api/admin/skills/review/{store.catalog_id}/reject", json={}
        )
        listed = await client.post(
            f"/api/admin/skills/store/{store.catalog_id}/listing",
            json={"listing": "listed"},
        )

    assert blank.status_code == 422
    assert whitespace.status_code == 422
    assert rejected.status_code == 422
    assert missing.status_code == 422
    # Putting something *on* the shelf needs no justification.
    assert listed.status_code == 200
    assert await _audits("admin.skill.listing", store.catalog_id) == 1


async def test_approval_is_audited_announced_and_idempotent(store):
    async with _client(store.admin) as client:
        first = await client.post(
            f"/api/admin/skills/review/{store.catalog_id}/approve"
        )
        again = await client.post(
            f"/api/admin/skills/review/{store.catalog_id}/approve"
        )

    assert first.status_code == 200
    assert first.json()["changed"] is True
    assert first.json()["listing"] == "listed"
    assert again.status_code == 200
    # The second click decided nothing, so it announced nothing.
    assert again.json()["changed"] is False
    assert await _audits("admin.skill.approve", store.catalog_id) == 1
    notices = await _notices(store.author["id"], "skill_approved")
    assert len(notices) == 1
    assert store.slug in notices[0].body


async def test_rejection_and_delisting_tell_the_author_why(store):
    async with _client(store.admin) as client:
        rejected = await client.post(
            f"/api/admin/skills/review/{store.catalog_id}/reject",
            json={"note": "The manifest promises a tool it never calls."},
        )
        relisted = await client.post(
            f"/api/admin/skills/store/{store.catalog_id}/listing",
            json={"listing": "listed"},
        )
        delisted = await client.post(
            f"/api/admin/skills/store/{store.catalog_id}/listing",
            json={"listing": "delisted", "note": "Broken after the 2.0 upgrade."},
        )
        repeat = await client.post(
            f"/api/admin/skills/store/{store.catalog_id}/listing",
            json={"listing": "delisted", "note": "Broken after the 2.0 upgrade."},
        )

    assert rejected.json()["listing"] == "rejected"
    assert relisted.json()["changed"] is True
    assert delisted.json()["listing"] == "delisted"
    assert repeat.json()["changed"] is False
    assert await _audits("admin.skill.reject", store.catalog_id) == 1
    # Two listing calls moved something; the repeat did not.
    assert await _audits("admin.skill.listing", store.catalog_id) == 2

    rejection = await _notices(store.author["id"], "skill_rejected")
    delisting = await _notices(store.author["id"], "skill_delisted")
    assert len(rejection) == 1
    assert "never calls" in rejection[0].body
    assert len(delisting) == 1
    assert "2.0 upgrade" in delisting[0].body
    assert delisting[0].workspace_id == store.author["default_workspace_id"]


async def test_featured_and_official_flags_are_audited(store, clean_overrides):
    async with _client(store.admin) as client:
        featured = await client.post(
            f"/api/admin/skills/store/{store.catalog_id}/featured",
            json={"featured": True},
        )
        official = await client.post(
            f"/api/admin/skills/store/{store.catalog_id}/official",
            json={"is_official": True},
        )
        shelved = await client.post(
            "/api/admin/skills/store/skill:anthropic-skills/listing",
            json={"listing": "listed"},
        )
        promoted = await client.post(
            "/api/admin/skills/store/skill:anthropic-skills/official",
            json={"is_official": True},
        )
        unknown = await client.post(
            "/api/admin/skills/store/community:nope/featured", json={"featured": True}
        )
        malformed = await client.post(
            "/api/admin/skills/store/nonsense/featured", json={"featured": True}
        )

    assert featured.json()["featured"] is True
    assert official.json()["is_official"] is True
    assert await _audits("admin.skill.featured", store.catalog_id) == 1
    assert await _audits("admin.skill.official", store.catalog_id) == 1
    # A code catalogue entry can be shelved but not claimed as our own work.
    assert shelved.json()["listing"] == "listed"
    assert promoted.status_code == 400
    assert unknown.status_code == 404
    assert malformed.status_code == 400


async def test_pinning_a_listed_package_does_not_announce_an_approval(store):
    """§4.7 announces shelf decisions, and pinning is not one.

    The regression this guards is quiet: every result reports the listing the
    row now sits on, so a `featured` toggle on an already-approved package
    looks exactly like an approval unless the announcement asks what moved.
    """
    async with _client(store.admin) as client:
        await client.post(f"/api/admin/skills/review/{store.catalog_id}/approve")
        assert len(await _notices(store.author["id"], "skill_approved")) == 1

        pinned = await client.post(
            f"/api/admin/skills/store/{store.catalog_id}/featured",
            json={"featured": True},
        )
        promoted = await client.post(
            f"/api/admin/skills/store/{store.catalog_id}/official",
            json={"is_official": True},
        )

    assert pinned.json()["changed"] is True
    assert promoted.json()["changed"] is True
    # Both moved something and were audited, but neither is news for the author.
    assert await _audits("admin.skill.featured", store.catalog_id) == 1
    assert await _audits("admin.skill.official", store.catalog_id) == 1
    assert len(await _notices(store.author["id"], "skill_approved")) == 1


async def test_review_detail_reads_the_manifest_without_extracting(store):
    async with _client(store.admin) as client:
        queue = await client.get("/api/admin/skills/review", params={"state": "pending"})
        detail = await client.get(f"/api/admin/skills/review/{store.catalog_id}")
        archive = await client.get(
            f"/api/admin/skills/review/{store.catalog_id}/archive"
        )
        catalogue = await client.get("/api/admin/skills/review/skill:web-research")

    assert store.catalog_id in {i["catalog_id"] for i in queue.json()["items"]}
    body = detail.json()
    assert body["archive_error"] is None
    assert "Say hello, once, and stop." in body["skill_md"]
    assert body["skill_md_truncated"] is False
    assert {entry["path"] for entry in body["files"]} == {
        f"{store.slug}/SKILL.md", f"{store.slug}/reference.md",
    }
    assert body["files_total"] == 2
    assert body["files_truncated"] is False
    assert body["author"]["username"] == store.author["username"]
    assert "archive_data" not in detail.text

    assert archive.status_code == 200
    assert archive.content == store.archive
    assert store.slug in archive.headers["content-disposition"]
    assert await _audits("admin.skill.download", store.catalog_id) == 1
    # A catalogue entry was never submitted, so it has nothing to review.
    assert catalogue.status_code == 404


async def test_a_disabled_authors_submission_is_still_reviewable(store):
    """The queue and the detail view must agree on what exists.

    Owner status is a *store* visibility rule: a disabled account's packages
    come off the shelf. It is not a moderation rule — the queue lists the row
    either way, and the submission whose author somebody already disabled is
    exactly the one a reviewer needs to open and read.
    """
    async with get_db_session() as session:
        await session.execute(
            sqlalchemy.update(User)
            .where(User.id == store.author["id"])
            .values(is_active=False)
        )

    async with _client(store.admin) as client:
        queue = await client.get("/api/admin/skills/review", params={"state": "pending"})
        detail = await client.get(f"/api/admin/skills/review/{store.catalog_id}")
        archive = await client.get(
            f"/api/admin/skills/review/{store.catalog_id}/archive"
        )
        rejected = await client.post(
            f"/api/admin/skills/review/{store.catalog_id}/reject",
            json={"note": "Account disabled for spam."},
        )

    assert store.catalog_id in {i["catalog_id"] for i in queue.json()["items"]}
    assert detail.status_code == 200
    assert "Say hello, once, and stop." in detail.json()["skill_md"]
    assert archive.status_code == 200
    assert archive.content == store.archive
    assert rejected.status_code == 200

    # Store visibility is untouched: the package stays unreachable to users.
    assert await user_library.get_published_skill(store.catalog_id) is None


async def test_review_detail_survives_awkward_archives(store):
    author = store.author["id"]
    crowded_slug = f"crowded-{store.suffix}"
    crowded = await _submit(author, crowded_slug, _zip(
        {f"{crowded_slug}/SKILL.md": SKILL_MD}
        | {f"{crowded_slug}/f{i}.txt": "x" for i in range(600)}
    ))
    bare_slug = f"bare-{store.suffix}"
    bare = await _submit(author, bare_slug, _zip(
        # The directory record is not a file and must not read as truncation.
        {f"{bare_slug}/": "", f"{bare_slug}/readme.md": "no manifest"}
    ))
    junk_slug = f"junk-{store.suffix}"
    junk = await _submit(author, junk_slug, b"this was never a zip file")

    async with _client(store.admin) as client:
        crowded_detail = await client.get(f"/api/admin/skills/review/{crowded}")
        bare_detail = await client.get(f"/api/admin/skills/review/{bare}")
        junk_detail = await client.get(f"/api/admin/skills/review/{junk}")

    assert crowded_detail.status_code == 200
    assert crowded_detail.json()["files_total"] == 601
    assert crowded_detail.json()["files_truncated"] is True
    assert len(crowded_detail.json()["files"]) == MAX_LISTED_FILES

    assert bare_detail.status_code == 200
    assert bare_detail.json()["skill_md"] is None
    assert "SKILL.md" in bare_detail.json()["skill_md_error"]
    assert bare_detail.json()["files_total"] == 1
    assert bare_detail.json()["files_truncated"] is False

    # A package that is not a ZIP is a reviewer's finding, not a 500.
    assert junk_detail.status_code == 200
    assert junk_detail.json()["archive_error"]
    assert junk_detail.json()["files"] == []


async def test_installs_list_both_halves_of_the_store(store):
    buyer = await PgUserRepo().create(
        id=f"askbuy-{store.suffix}", username=f"askbuy-{store.suffix}",
        password_hash="unused", email=f"buyer.{store.suffix}@skills.test",
    )
    await user_library.set_listing(store.catalog_id, user_library.LISTING_LISTED)
    await user_library.record_store_installation(
        user_id=buyer["id"], catalog_id=store.catalog_id,
        name=store.slug, install_dir=store.slug,
    )
    await user_library.record_store_installation(
        user_id=buyer["id"], catalog_id="mcp:playwright",
        name="playwright", install_dir="playwright",
    )

    async with _client(store.admin) as client:
        mine = await client.get(
            "/api/admin/skills/installs", params={"user_id": buyer["id"]}
        )
        by_entry = await client.get(
            "/api/admin/skills/installs", params={"catalog_id": "mcp:playwright"}
        )
        searched = await client.get(
            "/api/admin/skills/installs", params={"q": buyer["username"]}
        )
        store_view = await client.get(
            "/api/admin/skills/store", params={"q": store.slug}
        )

    rows = {item["catalog_id"]: item for item in mine.json()["items"]}
    assert set(rows) == {store.catalog_id, "mcp:playwright"}
    assert rows[store.catalog_id]["kind"] == "skill"
    assert rows[store.catalog_id]["origin"] == "community"
    assert rows[store.catalog_id]["user"]["email"] == buyer["email"]
    assert rows["mcp:playwright"]["kind"] == "mcp"
    assert rows["mcp:playwright"]["origin"] == "third_party"
    assert rows["mcp:playwright"]["title"] == "Playwright"

    assert [i["user"]["id"] for i in by_entry.json()["items"]] == [buyer["id"]]
    assert len(searched.json()["items"]) == 2
    assert store_view.json()["items"][0]["installs_count"] == 1


async def test_the_merged_store_page_is_bounded_and_counts_once(store, monkeypatch):
    """Merging two sources reads `offset + limit` rows; that has to stay finite.

    The community half is walked in windows, and every window used to re-ask
    the same `COUNT(*)` under the same filters. With no ceiling on `offset`
    either, the cost of one hand-edited URL grew with the table.
    """
    for index in range(2):
        await _submit(
            store.author["id"], f"extra-{index}-{store.suffix}",
            _zip({f"extra-{index}/SKILL.md": SKILL_MD}),
        )
    # One row per window, so a three-row window needs three passes.
    monkeypatch.setattr(admin_skills, "MERGE_PAGE", 1)

    statements: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = get_engine().sync_engine
    sqlalchemy.event.listen(engine, "before_cursor_execute", before)
    try:
        async with _client(store.admin) as client:
            page = await client.get(
                "/api/admin/skills/store",
                params={"q": store.suffix, "origin": "community",
                        "offset": 2, "limit": 1},
            )
            too_deep = await client.get(
                "/api/admin/skills/store",
                params={"offset": admin_skills.MAX_STORE_OFFSET + 1},
            )
    finally:
        sqlalchemy.event.remove(engine, "before_cursor_execute", before)

    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 3 and len(body["items"]) == 1
    counts = [
        text for text in statements
        if text.lstrip().upper().startswith("SELECT COUNT")
    ]
    assert len(counts) == 1, counts
    # A page nobody can assemble cheaply is refused rather than served slowly.
    assert too_deep.status_code == 422


async def test_no_list_view_ever_selects_an_archive(store):
    """§4.9: a page of a list must not drag published ZIPs off the database.

    Asserted against the emitted SQL rather than the response body: a
    projection that loads both blobs and then drops them from the JSON reads
    as correct from the outside while costing a request tens of megabytes.
    """
    buyer = await PgUserRepo().create(
        id=f"asksql-{store.suffix}", username=f"asksql-{store.suffix}",
        password_hash="unused",
    )
    # An installed submission, so the installs view joins a real user_skills
    # row rather than only exercising its outer join against nothing.
    await user_library.set_listing(store.catalog_id, user_library.LISTING_LISTED)
    await user_library.record_store_installation(
        user_id=buyer["id"], catalog_id=store.catalog_id,
        name=store.slug, install_dir=store.slug,
    )
    statements: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = get_engine().sync_engine
    sqlalchemy.event.listen(engine, "before_cursor_execute", before)
    try:
        async with _client(store.admin) as client:
            for path in ("store", "review", "installs"):
                assert (await client.get(f"/api/admin/skills/{path}")).status_code == 200
    finally:
        sqlalchemy.event.remove(engine, "before_cursor_execute", before)

    selects = [text for text in statements if text.lstrip().upper().startswith("SELECT")]
    assert selects, "the list views must actually hit the database"
    assert not any(
        column in text
        for text in selects
        for column in ("archive_data", "published_archive_data")
    )


async def test_reads_are_audited(store):
    async with _client(store.admin) as client:
        await client.get("/api/admin/skills/store")
        await client.get("/api/admin/skills/review")
        await client.get("/api/admin/skills/installs")
    async with get_db_session() as session:
        seen = await session.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "admin.view_skills",
                AuditLog.user_id == store.admin["user_id"],
            )
        )
    assert seen == 3
