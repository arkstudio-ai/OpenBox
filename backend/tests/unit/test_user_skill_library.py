"""User skill ownership, publication, and install-provenance boundaries."""

from __future__ import annotations

import json
import uuid

import pytest
import sqlalchemy
from sqlalchemy import delete, select, update

from db.base import get_db_session, get_engine
from db.models.catalog_override import CatalogOverride
from db.models.skill_install import SkillInstall
from db.models.user import User
from db.repository.user_repo import PgUserRepo
from skill.catalog import catalog_entry_id, catalog_index, load_catalog
from skill.user_library import (
    annotate_installed_skills,
    delete_owned_skill,
    get_owned_skill,
    get_published_skill,
    list_all_store_entries,
    list_owned_skills,
    list_published_catalog_entries,
    publish_personal_skill,
    record_store_installation,
    remove_store_installation,
    set_featured,
    set_listing,
    set_official,
    upsert_personal_snapshot,
    withdraw_personal_skill,
)


@pytest.fixture
async def library_users():
    suffix = uuid.uuid4().hex[:10]
    alice_id = f"user_skill_alice_{suffix}"
    bob_id = f"user_skill_bob_{suffix}"
    repo = PgUserRepo()
    await repo.create(
        id=alice_id,
        username=f"skill-alice-{suffix}",
        password_hash="unused",
    )
    await repo.create(
        id=bob_id,
        username=f"skill-bob-{suffix}",
        password_hash="unused",
    )
    return {
        "alice_id": alice_id,
        "alice_name": f"skill-alice-{suffix}",
        "bob_id": bob_id,
        "bob_name": f"skill-bob-{suffix}",
        "suffix": suffix,
    }


def _skill_info(slug: str, *, description: str = "A private writing workflow") -> dict:
    return {
        "name": slug,
        "install_dir": slug,
        "description": description,
        "icon": "✍️",
        "homepage": "https://example.test/skill",
        "requires_mcp": ["memory"],
        "files": ["references/style.md"],
        "source": "container",
        # These execution-time fields must never be copied to public metadata.
        "content": "private instructions",
        "base_dir": "/data/skills/private",
    }


async def _create_published(users: dict, label: str = "shared", **publish) -> tuple[str, dict]:
    slug = f"{label}-{users['suffix']}"
    created = await upsert_personal_snapshot(
        users["alice_id"],
        _skill_info(slug),
        b"PK\x03\x04first-snapshot",
    )
    published = await publish_personal_skill(users["alice_id"], slug, **publish)
    return slug, published


async def _catalog_ids() -> list[str]:
    entries = await list_published_catalog_entries()
    return [item["id"] for item in entries]


async def _entry(catalog_id: str) -> dict | None:
    return next(
        (item for item in await list_published_catalog_entries() if item["id"] == catalog_id),
        None,
    )


async def _capture_sql(factory):
    """Run one coroutine while recording every statement it sends the database."""
    statements: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = get_engine().sync_engine
    sqlalchemy.event.listen(engine, "before_cursor_execute", before)
    try:
        result = await factory()
    finally:
        sqlalchemy.event.remove(engine, "before_cursor_execute", before)
    return result, statements


@pytest.fixture
async def clean_overrides():
    """Catalogue overrides are global rows; do not leak them between tests."""
    yield
    async with get_db_session() as session:
        await session.execute(delete(CatalogOverride))


async def test_personal_snapshots_are_private_and_resolve_all_owned_identifiers(library_users):
    users = library_users
    slug = f"private-{users['suffix']}"
    archive = b"PK\x03\x04private-snapshot"
    created = await upsert_personal_snapshot(users["alice_id"], _skill_info(slug), archive)

    assert created["publication_status"] == "unpublished"
    assert created["category"] == "personal"
    assert "archive_data" not in created
    assert "archive_sha256" not in created

    # The same slug belongs to a completely separate library row for another
    # user; owner/name uniqueness is scoped to the owner, not system-wide.
    bob_created = await upsert_personal_snapshot(
        users["bob_id"],
        _skill_info(slug, description="Bob's unrelated private workflow"),
        b"PK\x03\x04bob-private-snapshot",
    )
    assert bob_created["id"] != created["id"]

    for identifier in (created["id"], created["name"], created["install_dir"]):
        found = await get_owned_skill(users["alice_id"], identifier)
        assert found is not None
        assert found["id"] == created["id"]

    assert await get_owned_skill(users["bob_id"], created["id"]) is None
    bob_owned = await get_owned_skill(users["bob_id"], slug)
    assert bob_owned is not None
    assert bob_owned["id"] == bob_created["id"]
    assert await get_published_skill(f"community:{created['id']}") is None
    assert all(
        item["id"] != f"community:{created['id']}"
        for item in await list_published_catalog_entries()
    )

    owner_download = await get_owned_skill(users["alice_id"], slug, include_archive=True)
    assert owner_download is not None
    assert owner_download["archive_data"] == archive
    assert len(owner_download["archive_sha256"]) == 64

    owned = await list_owned_skills(users["alice_id"])
    listed = next(item for item in owned if item["id"] == created["id"])
    assert listed["restore_available"] is True
    assert listed["draft_version"] == 1
    assert listed["published_version"] is None
    assert listed["has_unpublished_changes"] is True
    assert not (
        {
            "archive_data",
            "archive_sha256",
            "published_archive_data",
            "published_archive_sha256",
            "metadata_data",
            "owner_id",
        }
        & listed.keys()
    )
    json.dumps(owned)
    assert all(item["id"] != bob_created["id"] for item in owned)


async def test_published_catalog_is_safe_public_json_and_publish_is_owner_only(library_users):
    users = library_users
    slug, published = await _create_published(users)
    catalog_id = f"community:{published['id']}"

    with pytest.raises(LookupError):
        await publish_personal_skill(users["bob_id"], published["id"])

    entries = await list_published_catalog_entries()
    entry = next(item for item in entries if item["id"] == catalog_id)
    assert entry["community"] is True
    assert entry["kind"] == "skill"
    assert entry["publisher"] == users["alice_name"]
    assert entry["requires_mcp"] == ["memory"]
    assert entry["missing_mcp"] == ["memory"]
    assert entry["install"] == {}
    assert not ({"archive_data", "archive_sha256", "metadata_data", "owner_id"} & entry.keys())
    json.dumps(entry)  # public catalogue entries must always be JSON serializable

    public = await get_published_skill(catalog_id)
    assert public is not None
    assert "archive_data" not in public
    installer_copy = await get_published_skill(catalog_id, include_archive=True)
    assert installer_copy is not None
    assert installer_copy["archive_data"] == b"PK\x03\x04first-snapshot"

    # Merely re-snapshotting identical bytes is idempotent and must not revoke
    # an explicitly published snapshot.
    unchanged = await upsert_personal_snapshot(
        users["alice_id"],
        _skill_info(slug),
        b"PK\x03\x04first-snapshot",
    )
    assert unchanged["publication_status"] == "published"
    assert unchanged["version"] == published["version"]
    assert unchanged["published_version"] == published["published_version"]
    assert unchanged["has_unpublished_changes"] is False
    assert unchanged["published_at"] == published["published_at"]


async def test_install_provenance_is_per_user_and_categories_are_not_guessed(library_users):
    users = library_users
    slug, published = await _create_published(users, "provenance")
    catalog_id = f"community:{published['id']}"

    recorded = await record_store_installation(
        user_id=users["bob_id"],
        catalog_id=catalog_id,
        name=slug,
        install_dir=slug,
    )
    assert recorded["category"] == "store"
    assert recorded["catalog_id"] == catalog_id

    scanned = [
        {"name": slug, "install_dir": slug, "source": "container"},
        {"name": "manual-copy", "install_dir": "manual-copy", "source": "container"},
        {"name": "dev-browser", "install_dir": "dev-browser", "source": "builtin"},
        {"name": "host-helper", "source": "project"},
    ]
    bob = await annotate_installed_skills(users["bob_id"], scanned)
    assert [item["category"] for item in bob] == ["store", "installed", "builtin", "host"]
    assert bob[0]["catalog_id"] == catalog_id
    assert bob[0]["publication_status"] is None

    alice = await annotate_installed_skills(users["alice_id"], scanned[:1])
    assert alice[0]["category"] == "personal"
    assert alice[0]["library_id"] == published["id"]
    assert alice[0]["publication_status"] == "published"
    assert alice[0]["catalog_id"] == catalog_id

    # One user cannot remove another user's provenance row.
    assert await remove_store_installation(users["alice_id"], slug) is False
    still_store = await annotate_installed_skills(users["bob_id"], scanned[:1])
    assert still_store[0]["category"] == "store"

    assert await remove_store_installation(users["bob_id"], catalog_id) is True
    no_longer_claimed = await annotate_installed_skills(users["bob_id"], scanned[:1])
    assert no_longer_claimed[0]["category"] == "installed"


async def test_store_install_provenance_wins_over_a_stale_owned_slug(library_users):
    users = library_users
    slug = f"reused-slug-{users['suffix']}"
    await upsert_personal_snapshot(
        users["alice_id"],
        _skill_info(slug, description="Alice's uninstalled durable snapshot"),
        b"PK\x03\x04alice-old-copy",
    )
    bob_created = await upsert_personal_snapshot(
        users["bob_id"],
        _skill_info(slug, description="Bob's public package"),
        b"PK\x03\x04bob-public-copy",
    )
    bob_published = await publish_personal_skill(users["bob_id"], bob_created["id"])
    await record_store_installation(
        user_id=users["alice_id"],
        catalog_id=f"community:{bob_published['id']}",
        name=slug,
        install_dir=slug,
    )

    listed = await annotate_installed_skills(
        users["alice_id"],
        [{"name": slug, "install_dir": slug, "source": "container"}],
    )
    assert listed[0]["category"] == "store"
    assert listed[0]["catalog_id"] == f"community:{bob_published['id']}"
    assert listed[0]["library_id"] is None


async def test_draft_refresh_preserves_public_release_until_explicit_publish(library_users):
    users = library_users
    slug, published = await _create_published(users, "revised")
    catalog_id = f"community:{published['id']}"
    await record_store_installation(
        user_id=users["bob_id"],
        catalog_id=catalog_id,
        name=slug,
        install_dir=slug,
    )

    revised = await upsert_personal_snapshot(
        users["alice_id"],
        _skill_info(slug, description="A materially revised workflow"),
        b"PK\x03\x04second-snapshot",
    )
    assert revised["id"] == published["id"]
    assert revised["version"] == published["version"] + 1
    assert revised["publication_status"] == "published"
    assert revised["published_at"] == published["published_at"]
    assert revised["catalog_id"] == catalog_id
    assert revised["published_version"] == published["published_version"]
    assert revised["has_unpublished_changes"] is True

    # Owner restore/download receives the refreshed draft, while installers
    # and the catalogue continue to receive the immutable first release.
    owner_draft = await get_owned_skill(
        users["alice_id"], slug, include_archive=True
    )
    assert owner_draft is not None
    assert owner_draft["archive_data"] == b"PK\x03\x04second-snapshot"
    old_public = await get_published_skill(catalog_id, include_archive=True)
    assert old_public is not None
    assert old_public["archive_data"] == b"PK\x03\x04first-snapshot"
    assert old_public["description"] == "A private writing workflow"
    entry = next(
        item
        for item in await list_published_catalog_entries()
        if item["id"] == catalog_id
    )
    assert entry["description"] == "A private writing workflow"
    assert entry["version"] == published["published_version"]

    owner = await annotate_installed_skills(
        users["alice_id"],
        [{"name": slug, "install_dir": slug, "source": "container"}],
    )
    assert owner[0]["category"] == "personal"
    assert owner[0]["publication_status"] == "published"

    # Existing users keep truthful install provenance for the copy already in
    # their sandbox even while this publisher prepares a new version.
    installer = await annotate_installed_skills(
        users["bob_id"],
        [{"name": slug, "install_dir": slug, "source": "container"}],
    )
    assert installer[0]["category"] == "store"
    assert installer[0]["catalog_id"] == catalog_id

    updated_release = await publish_personal_skill(users["alice_id"], slug)
    assert updated_release["published_version"] == published["published_version"] + 1
    assert updated_release["has_unpublished_changes"] is False
    assert updated_release["published_at"] != published["published_at"]

    new_public = await get_published_skill(catalog_id, include_archive=True)
    assert new_public is not None
    assert new_public["archive_data"] == b"PK\x03\x04second-snapshot"
    assert new_public["description"] == "A materially revised workflow"
    assert new_public["version"] == updated_release["published_version"]


async def test_public_access_matches_catalog_owner_status(library_users):
    users = library_users
    _, published = await _create_published(users, "owner-status")
    catalog_id = f"community:{published['id']}"

    async def set_owner_status(*, active: bool, deleted: bool) -> None:
        async with get_db_session() as session:
            await session.execute(
                update(User)
                .where(User.id == users["alice_id"])
                .values(is_active=active, is_deleted=deleted)
            )

    await set_owner_status(active=False, deleted=False)
    assert await get_published_skill(catalog_id, include_archive=True) is None
    assert all(
        item["id"] != catalog_id for item in await list_published_catalog_entries()
    )

    await set_owner_status(active=True, deleted=True)
    assert await get_published_skill(catalog_id, include_archive=True) is None
    assert all(
        item["id"] != catalog_id for item in await list_published_catalog_entries()
    )

    await set_owner_status(active=True, deleted=False)
    assert await get_published_skill(catalog_id) is not None


async def test_delete_owned_skill_is_owner_only_and_cascades_install_provenance(
    library_users,
):
    users = library_users
    slug, published = await _create_published(users, "delete-owned")
    catalog_id = f"community:{published['id']}"
    await record_store_installation(
        user_id=users["bob_id"],
        catalog_id=catalog_id,
        name=slug,
        install_dir=slug,
    )

    assert await delete_owned_skill(users["bob_id"], catalog_id) is False
    assert await get_published_skill(catalog_id) is not None

    assert await delete_owned_skill(users["alice_id"], catalog_id) is True
    assert await get_owned_skill(users["alice_id"], slug) is None
    assert await get_published_skill(catalog_id) is None
    assert all(
        item["id"] != catalog_id for item in await list_published_catalog_entries()
    )

    scanned = await annotate_installed_skills(
        users["bob_id"],
        [{"name": slug, "install_dir": slug, "source": "container"}],
    )
    assert scanned[0]["category"] == "installed"


# ── the listing state machine ──


async def test_review_queues_a_submission_until_an_operator_lists_it(library_users):
    users = library_users
    slug, submitted = await _create_published(users, "queued", review_required=True)
    catalog_id = f"community:{submitted['id']}"

    # Published by its author, but nobody else can see or install it yet: the
    # two axes are separate on purpose.
    assert submitted["publication_status"] == "published"
    assert submitted["listing"] == "pending"
    assert submitted["is_official"] is False
    assert catalog_id not in await _catalog_ids()
    assert await get_published_skill(catalog_id) is None
    # The review console still needs the exact bytes under review.
    under_review = await get_published_skill(
        catalog_id, include_archive=True, require_listed=False
    )
    assert under_review is not None
    assert under_review["archive_data"] == b"PK\x03\x04first-snapshot"
    assert under_review["listing"] == "pending"

    rejected = await set_listing(
        catalog_id, "rejected", note="Instructions call a tool it never installs.",
        actor_user_id="admin-1",
    )
    assert rejected["changed"] is True
    assert rejected["previous"] == "pending"
    assert rejected["owner_id"] == users["alice_id"]
    assert rejected["name"] == slug
    assert catalog_id not in await _catalog_ids()

    author_view = await get_owned_skill(users["alice_id"], slug)
    assert author_view["listing"] == "rejected"
    assert author_view["listing_note"] == "Instructions call a tool it never installs."

    # Resubmitting after a rejection queues again rather than going straight
    # back on the shelf, and the stale verdict does not follow it.
    resubmitted = await publish_personal_skill(
        users["alice_id"], slug, review_required=True
    )
    assert resubmitted["listing"] == "pending"
    assert resubmitted["listing_note"] is None

    approved = await set_listing(catalog_id, "listed", actor_user_id="admin-1")
    assert approved["changed"] is True
    assert approved["previous"] == "pending"
    assert catalog_id in await _catalog_ids()
    assert await get_published_skill(catalog_id) is not None


async def test_an_admin_publisher_skips_review_and_lands_on_the_official_shelf(
    library_users,
):
    users = library_users
    _, published = await _create_published(
        users, "official", review_required=True, publisher_is_admin=True
    )
    catalog_id = f"community:{published['id']}"

    assert published["listing"] == "listed"
    assert published["is_official"] is True
    entry = await _entry(catalog_id)
    assert entry is not None
    assert entry["origin"] == "official"
    assert entry["official"] is True


async def test_a_new_version_cannot_lift_an_operators_delisting(library_users):
    users = library_users
    slug, published = await _create_published(users, "delisted", review_required=False)
    catalog_id = f"community:{published['id']}"
    assert published["listing"] == "listed"

    delisted = await set_listing(
        catalog_id, "delisted", note="Duplicates an official skill.",
        actor_user_id="admin-1",
    )
    assert delisted["changed"] is True
    assert catalog_id not in await _catalog_ids()

    # The whole point of the rule: pushing an update with review off is not an
    # appeal, so the package stays off the shelf and keeps its reason.
    await upsert_personal_snapshot(
        users["alice_id"], _skill_info(slug, description="Now with a fix"),
        b"PK\x03\x04second-snapshot",
    )
    republished = await publish_personal_skill(
        users["alice_id"], slug, review_required=False
    )
    assert republished["listing"] == "delisted"
    assert republished["listing_note"] == "Duplicates an official skill."
    assert republished["published_version"] == published["published_version"] + 1
    assert catalog_id not in await _catalog_ids()

    # With review on the update goes back to a human instead — still not to the
    # shelf on the author's own say-so.
    queued = await publish_personal_skill(
        users["alice_id"], slug, review_required=True
    )
    assert queued["listing"] == "pending"
    assert catalog_id not in await _catalog_ids()

    relisted = await set_listing(catalog_id, "listed", actor_user_id="admin-1")
    assert relisted["previous"] == "pending"
    assert catalog_id in await _catalog_ids()


async def test_withdrawal_hides_a_release_without_destroying_it(library_users):
    users = library_users
    slug, published = await _create_published(users, "withdrawn", review_required=False)
    catalog_id = f"community:{published['id']}"
    await record_store_installation(
        user_id=users["bob_id"],
        catalog_id=catalog_id,
        user_skill_id=published["id"],
        name=slug,
        install_dir=slug,
    )

    withdrawn = await withdraw_personal_skill(users["alice_id"], slug)
    assert withdrawn["publication_status"] == "withdrawn"
    # The operator's decision and the release itself both survive: a withdrawal
    # is the author pausing, not the row being erased.
    assert withdrawn["listing"] == "listed"
    assert withdrawn["published_version"] == published["published_version"]
    assert catalog_id not in await _catalog_ids()
    assert await get_published_skill(catalog_id) is None

    # An existing installation is untouched by anything the author does now.
    installed = await annotate_installed_skills(
        users["bob_id"], [{"name": slug, "install_dir": slug, "source": "container"}]
    )
    assert installed[0]["category"] == "store"
    assert installed[0]["catalog_id"] == catalog_id

    # Withdrawing twice is a no-op, and editing the draft afterwards does not
    # quietly un-withdraw the package.
    assert (await withdraw_personal_skill(users["alice_id"], slug))[
        "publication_status"
    ] == "withdrawn"
    await upsert_personal_snapshot(
        users["alice_id"], _skill_info(slug, description="Edited while withdrawn"),
        b"PK\x03\x04edited-while-withdrawn",
    )
    assert (await get_owned_skill(users["alice_id"], slug))[
        "publication_status"
    ] == "withdrawn"

    republished = await publish_personal_skill(
        users["alice_id"], slug, review_required=False
    )
    assert republished["publication_status"] == "published"
    assert catalog_id in await _catalog_ids()


async def test_re_publishing_unchanged_bytes_does_not_burn_a_version(library_users):
    users = library_users
    slug, published = await _create_published(users, "unchanged", review_required=False)
    await withdraw_personal_skill(users["alice_id"], slug)

    resubmitted = await publish_personal_skill(
        users["alice_id"], slug, review_required=False
    )
    assert resubmitted["published_version"] == published["published_version"]
    assert resubmitted["published_at"] == published["published_at"]
    assert resubmitted["has_unpublished_changes"] is False


async def test_withdraw_is_owner_scoped_and_needs_something_to_withdraw(library_users):
    users = library_users
    slug, published = await _create_published(users, "withdraw-guard")

    with pytest.raises(LookupError):
        await withdraw_personal_skill(users["bob_id"], published["id"])

    draft_slug = f"never-published-{users['suffix']}"
    await upsert_personal_snapshot(
        users["alice_id"], _skill_info(draft_slug), b"PK\x03\x04draft-only"
    )
    with pytest.raises(ValueError):
        await withdraw_personal_skill(users["alice_id"], draft_slug)
    # A draft that was never submitted has no listing to report at all.
    assert (await get_owned_skill(users["alice_id"], draft_slug))["listing"] is None


# ── operator mutations ──


async def test_operator_mutations_report_the_change_and_repeat_harmlessly(library_users):
    users = library_users
    slug, published = await _create_published(users, "moderated", review_required=True)
    catalog_id = f"community:{published['id']}"

    listed = await set_listing(catalog_id, "listed", actor_user_id="admin-1")
    assert (listed["changed"], listed["previous"], listed["current"]) == (
        True, "pending", "listed",
    )
    assert listed["source"] == "community"
    # Enough for the caller to audit and notify without a second query.
    assert listed["workspace_id"] and listed["owner_id"] == users["alice_id"]
    assert listed["version"] == published["published_version"]

    # A second operator clicking the same button must not produce a second
    # audit entry or a second notification for the author.
    again = await set_listing(catalog_id, "listed", actor_user_id="admin-2")
    assert again["changed"] is False
    assert again["listing"] == "listed"

    pinned = await set_featured(catalog_id, True, actor_user_id="admin-1")
    assert (pinned["field"], pinned["previous"], pinned["current"]) == (
        "featured", False, True,
    )
    repeated = await set_featured(catalog_id, True)
    assert (repeated["changed"], repeated["current"]) == (False, True)
    assert (await _entry(catalog_id))["featured"] is True

    promoted = await set_official(catalog_id, True, actor_user_id="admin-1")
    assert (promoted["field"], promoted["changed"]) == ("official", True)
    assert (await set_official(catalog_id, True))["changed"] is False
    assert (await _entry(catalog_id))["origin"] == "official"
    demoted = await set_official(catalog_id, False, actor_user_id="admin-1")
    assert demoted["changed"] is True
    assert (await _entry(catalog_id))["origin"] == "community"


async def test_operator_mutations_reject_targets_they_cannot_act_on(library_users):
    users = library_users
    _, published = await _create_published(users, "guarded")
    catalog_id = f"community:{published['id']}"

    assert await set_listing("community:skill_does_not_exist", "listed") is None
    assert await set_featured("community:skill_does_not_exist", True) is None
    assert await set_official("community:skill_does_not_exist", True) is None

    for bad in ("", "no-prefix", "wat:thing", "skill:"):
        with pytest.raises(ValueError):
            await set_listing(bad, "listed")

    with pytest.raises(ValueError):
        await set_listing(catalog_id, "shelved")
    # Submission states have no meaning for an entry that lives in code.
    with pytest.raises(ValueError):
        await set_listing("skill:web-research", "pending")
    # Claiming somebody else's published work as official is not a toggle.
    with pytest.raises(ValueError):
        await set_official("mcp:playwright", True)


# ── the code catalogue and its overrides ──


async def test_catalog_ships_shelf_defaults_and_the_database_overrides_them(
    clean_overrides,
):
    catalog = await load_catalog()
    by_id = {entry["catalog_id"]: entry for entry in catalog["skills"] + catalog["mcp"]}

    # Q3: the Anthropic pack ships off the shelf, the MCP servers the official
    # skills depend on ship on it.
    assert by_id["skill:anthropic-skills"]["listing"] == "delisted"
    assert by_id["skill:web-research"]["listing"] == "listed"
    assert by_id["mcp:playwright"]["listing"] == "listed"
    assert by_id["skill:web-research"]["origin"] == "official"
    assert by_id["skill:web-research"]["official"] is True
    assert by_id["skill:anthropic-skills"]["origin"] == "third_party"
    assert by_id["mcp:playwright"]["origin"] == "third_party"
    assert by_id["mcp:playwright"]["featured"] is False

    # Asking for the state an entry already has writes nothing at all: with no
    # row, the code default is the answer.
    unchanged = await set_listing("skill:web-research", "listed", actor_user_id="admin-1")
    assert unchanged["changed"] is False
    async with get_db_session() as session:
        assert (await session.execute(select(CatalogOverride))).scalars().all() == []

    shelved = await set_listing(
        "skill:anthropic-skills", "listed", actor_user_id="admin-1"
    )
    assert (shelved["changed"], shelved["previous"], shelved["current"]) == (
        True, "delisted", "listed",
    )
    removed = await set_listing(
        "mcp:firecrawl", "delisted", note="Key rotation in progress.",
        actor_user_id="admin-1",
    )
    assert removed["changed"] is True
    pinned = await set_featured("mcp:playwright", True, actor_user_id="admin-1")
    assert pinned["changed"] is True

    overlaid = await load_catalog()
    by_id = {e["catalog_id"]: e for e in overlaid["skills"] + overlaid["mcp"]}
    assert by_id["skill:anthropic-skills"]["listing"] == "listed"
    assert by_id["mcp:firecrawl"]["listing"] == "delisted"
    assert by_id["mcp:firecrawl"]["listing_note"] == "Key rotation in progress."
    # Featuring an entry must not disturb the shelf state it already had.
    assert by_id["mcp:playwright"]["featured"] is True
    assert by_id["mcp:playwright"]["listing"] == "listed"

    # Dependency resolution deliberately ignores all of this: an official skill
    # that requires firecrawl must still be installable with its server.
    assert catalog_index()[catalog_entry_id("mcp", "firecrawl")]["name"] == "firecrawl"
    assert set(catalog_index()) >= {"skill:web-research", "mcp:firecrawl"}


# ── install provenance across both halves of the store ──


async def test_catalogue_installs_are_recorded_and_counted_with_community_ones(
    library_users,
):
    users = library_users
    slug, published = await _create_published(users, "counted", review_required=False)
    catalog_id = f"community:{published['id']}"

    assert (await _entry(catalog_id))["installs_count"] == 0
    for user in ("alice_id", "bob_id"):
        await record_store_installation(
            user_id=users[user],
            catalog_id=catalog_id,
            user_skill_id=published["id"],
            name=slug,
            install_dir=slug,
        )
    assert (await _entry(catalog_id))["installs_count"] == 2

    # A catalogue entry has no user_skills row behind it, so its key is the
    # only identity it has.
    mcp_dir = f"playwright-{users['suffix']}"
    recorded = await record_store_installation(
        user_id=users["bob_id"],
        catalog_id=catalog_entry_id("mcp", "playwright"),
        name="playwright",
        install_dir=mcp_dir,
    )
    assert recorded["catalog_id"] == "mcp:playwright"
    assert recorded["kind"] == "mcp"

    skill_dir = f"web-research-{users['suffix']}"
    await record_store_installation(
        user_id=users["bob_id"],
        catalog_id="skill:web-research",
        kind="skill",
        name="web-research",
        install_dir=skill_dir,
    )
    annotated = await annotate_installed_skills(
        users["bob_id"],
        [{"name": "web-research", "install_dir": skill_dir, "source": "container"}],
    )
    assert annotated[0]["category"] == "store"
    assert annotated[0]["catalog_id"] == "skill:web-research"

    with pytest.raises(ValueError):
        await record_store_installation(
            user_id=users["bob_id"], catalog_id="mcp:playwright", kind="skill",
            name="playwright", install_dir=f"mismatch-{users['suffix']}",
        )

    # Uninstalling one kind must not take the same-named other kind with it.
    assert await remove_store_installation(users["bob_id"], "playwright", kind="skill") is False
    assert await remove_store_installation(users["bob_id"], "mcp:playwright") is True
    assert await remove_store_installation(users["bob_id"], skill_dir) is True


async def test_an_mcp_server_does_not_take_over_a_same_named_skills_provenance(
    library_users,
):
    """``install_dir`` carries two namespaces, and they do collide.

    A skill's sandbox directory and an MCP server's name share the column: the
    catalogue ships servers called "memory" and "filesystem", and nothing stops
    a community skill from being called that too. Keyed on the directory alone,
    installing the server silently re-pointed the skill's row — the author lost
    an install, the console lost the installer, and the eventual uninstall
    matched nothing.
    """
    users = library_users
    slug, published = await _create_published(users, "memory", review_required=False)
    catalog_id = f"community:{published['id']}"

    await record_store_installation(
        user_id=users["bob_id"], catalog_id=catalog_id,
        user_skill_id=published["id"], name=slug, install_dir=slug,
    )
    # The server is addressed by its name, which is also its "directory".
    await record_store_installation(
        user_id=users["bob_id"], catalog_id=catalog_entry_id("mcp", "memory"),
        name=slug, install_dir=slug,
    )

    async with get_db_session() as session:
        rows = {
            row.kind: row.catalog_id
            for row in (await session.execute(
                select(SkillInstall).where(
                    SkillInstall.user_id == users["bob_id"],
                    SkillInstall.install_dir == slug,
                )
            )).scalars()
        }
    assert rows == {"skill": catalog_id, "mcp": "mcp:memory"}
    # The author's install count survives the server install.
    assert (await _entry(catalog_id))["installs_count"] == 1

    # A sandbox skill listing must be annotated from the skill row, never the
    # server row that happens to share the directory.
    annotated = await annotate_installed_skills(
        users["bob_id"],
        [{"name": slug, "install_dir": slug, "source": "container"}],
    )
    assert annotated[0]["category"] == "store"
    assert annotated[0]["catalog_id"] == catalog_id

    # And each uninstall removes only its own kind.
    assert await remove_store_installation(users["bob_id"], slug, kind="skill") is True
    assert (await _entry(catalog_id))["installs_count"] == 0
    assert await remove_store_installation(users["bob_id"], slug, kind="mcp") is True


async def test_the_store_is_closed_to_anything_the_visibility_rule_excludes(
    library_users,
):
    users = library_users
    slug, published = await _create_published(users, "gated", review_required=False)
    catalog_id = f"community:{published['id']}"

    async def install_attempt():
        return await record_store_installation(
            user_id=users["bob_id"],
            catalog_id=catalog_id,
            user_skill_id=published["id"],
            name=slug,
            install_dir=f"gated-copy-{users['suffix']}",
        )

    for state in ("pending", "rejected", "delisted"):
        await set_listing(catalog_id, state, note="because", actor_user_id="admin-1")
        assert catalog_id not in await _catalog_ids()
        assert await get_published_skill(catalog_id) is None
        with pytest.raises(LookupError):
            await install_attempt()

    await set_listing(catalog_id, "listed", actor_user_id="admin-1")
    await withdraw_personal_skill(users["alice_id"], slug)
    assert catalog_id not in await _catalog_ids()
    with pytest.raises(LookupError):
        await install_attempt()

    await publish_personal_skill(users["alice_id"], slug, review_required=False)
    assert (await install_attempt())["catalog_id"] == catalog_id


# ── the operator's list ──


async def test_the_operator_list_shows_every_state_without_loading_archives(
    library_users,
):
    users = library_users
    slug, published = await _create_published(users, "console", review_required=False)
    catalog_id = f"community:{published['id']}"
    await record_store_installation(
        user_id=users["bob_id"], catalog_id=catalog_id,
        user_skill_id=published["id"], name=slug, install_dir=slug,
    )
    # Back into the queue, so the console list is exercised on a row the store
    # itself is hiding.
    listed_workspace = (await set_listing(catalog_id, "pending"))["workspace_id"]

    page, statements = await _capture_sql(lambda: list_all_store_entries(query=slug))
    assert page["total"] == 1
    entry = page["entries"][0]
    assert entry["catalog_id"] == catalog_id
    assert entry["listing"] == "pending"
    assert entry["status"] == "published"
    assert entry["author"] == {
        "user_id": users["alice_id"],
        "username": users["alice_name"],
        "email": None,
    }
    assert entry["workspace_id"] == listed_workspace
    assert entry["installs_count"] == 1
    assert entry["size"] == len(b"PK\x03\x04first-snapshot")
    assert len(entry["sha256"]) == 64
    assert entry["requires_mcp"] == ["memory"]
    json.dumps(page)

    # A page of the admin list would otherwise drag every published ZIP
    # through the connection to render a table of names.
    assert statements, "the listing must actually hit the database"
    assert not any("archive_data" in statement for statement in statements)

    # Searching by the author, not just by the package.
    by_author = await list_all_store_entries(query=users["alice_name"])
    assert catalog_id in {item["catalog_id"] for item in by_author["entries"]}

    # Every listing state stays visible to the console; the store's own
    # projection is the one that filters.
    await set_listing(catalog_id, "rejected", note="No.", actor_user_id="admin-1")
    rejected = await list_all_store_entries(listing="rejected", query=slug)
    assert rejected["total"] == 1
    assert rejected["entries"][0]["listing_note"] == "No."
    assert (await list_all_store_entries(listing="listed", query=slug))["total"] == 0

    await withdraw_personal_skill(users["alice_id"], slug)
    assert (await list_all_store_entries(status="withdrawn", query=slug))["total"] == 1

    # A draft nobody ever submitted is not a store entry.
    draft_slug = f"unsubmitted-{users['suffix']}"
    await upsert_personal_snapshot(
        users["alice_id"], _skill_info(draft_slug), b"PK\x03\x04never-sent"
    )
    assert (await list_all_store_entries(query=draft_slug))["total"] == 0

    for bad in ({"listing": "nope"}, {"status": "nope"}, {"sort": "nope"}):
        with pytest.raises(ValueError):
            await list_all_store_entries(**bad)


async def test_the_operator_list_pages_and_orders_deterministically(library_users):
    users = library_users
    label = f"paged-{users['suffix']}"
    for index in range(3):
        slug = f"{label}-{index}"
        await upsert_personal_snapshot(
            users["alice_id"], _skill_info(slug), f"PK\x03\x04page-{index}".encode()
        )
        await publish_personal_skill(users["alice_id"], slug, review_required=False)

    first = await list_all_store_entries(query=label, sort="oldest", limit=2)
    assert first["total"] == 3
    assert [item["name"] for item in first["entries"]] == [
        f"{label}-0", f"{label}-1",
    ]
    second = await list_all_store_entries(query=label, sort="oldest", limit=2, offset=2)
    assert [item["name"] for item in second["entries"]] == [f"{label}-2"]
    # The review queue reads oldest-first; the store reads newest-first.
    newest = await list_all_store_entries(query=label, sort="recent")
    assert newest["entries"][0]["name"] == f"{label}-2"
