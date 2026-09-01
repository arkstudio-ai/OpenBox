"""User skill ownership, publication, and install-provenance boundaries."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select, update

from db.base import get_db_session
from db.models.user import User
from db.models.user_skill import UserSkill
from db.repository.user_repo import PgUserRepo
from skill.user_library import (
    annotate_installed_skills,
    delete_owned_skill,
    get_owned_skill,
    get_published_skill,
    list_owned_skills,
    list_published_catalog_entries,
    publish_personal_skill,
    record_community_installation,
    remove_community_installation,
    upsert_personal_snapshot,
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


async def _create_published(users: dict, label: str = "shared") -> tuple[str, dict]:
    slug = f"{label}-{users['suffix']}"
    created = await upsert_personal_snapshot(
        users["alice_id"],
        _skill_info(slug),
        b"PK\x03\x04first-snapshot",
    )
    published = await publish_personal_skill(users["alice_id"], slug)
    return slug, published


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

    recorded = await record_community_installation(
        user_id=users["bob_id"],
        user_skill_id=published["id"],
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
    assert await remove_community_installation(users["alice_id"], slug) is False
    still_store = await annotate_installed_skills(users["bob_id"], scanned[:1])
    assert still_store[0]["category"] == "store"

    assert await remove_community_installation(users["bob_id"], catalog_id) is True
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
    await record_community_installation(
        user_id=users["alice_id"],
        user_skill_id=bob_published["id"],
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
    await record_community_installation(
        user_id=users["bob_id"],
        user_skill_id=published["id"],
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
    await record_community_installation(
        user_id=users["bob_id"],
        user_skill_id=published["id"],
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


async def test_deleted_skill_keeps_monotonic_tombstone_until_explicit_recreate(
    library_users,
):
    users = library_users
    slug = f"lifecycle-{users['suffix']}"
    created = await upsert_personal_snapshot(
        users["alice_id"],
        _skill_info(slug),
        b"PK\x03\x04generation-one",
    )
    assert created["lifecycle_generation"] == 1

    assert await delete_owned_skill(users["alice_id"], slug) is True
    assert await get_owned_skill(users["alice_id"], slug) is None
    assert all(
        item["id"] != created["id"]
        for item in await list_owned_skills(users["alice_id"])
    )

    async with get_db_session() as session:
        tombstone = (
            await session.execute(
                select(UserSkill).where(UserSkill.id == created["id"])
            )
        ).scalar_one()
        assert tombstone.lifecycle_state == "deleted"
        assert tombstone.lifecycle_generation == 2
        assert tombstone.archive_data == b""
        assert tombstone.published_archive_data is None

    recreated = await upsert_personal_snapshot(
        users["alice_id"],
        _skill_info(slug, description="A deliberate new package"),
        b"PK\x03\x04generation-three",
    )
    assert recreated["id"] == created["id"]
    assert recreated["lifecycle_generation"] == 3
    assert recreated["publication_status"] == "unpublished"
