"""API-level community Skill flow across two users and one immutable ZIP."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from api import metadata
from core.config import get_config
from db.base import get_db_session
from db.models.notification import Notification
from db.models.skill_install import SkillInstall
from db.repository.user_repo import PgUserRepo
from skill.user_library import (
    annotate_installed_skills,
    get_owned_skill,
    publish_personal_skill,
    record_store_installation,
    upsert_personal_snapshot,
)


class FakeSkillSandbox:
    def __init__(self, *, installed: list[dict] | None = None):
        self.installed = installed or []
        self.uploads: list[tuple[bytes, str, str]] = []
        self.uninstalls: list[str] = []
        self.installs: list[dict] = []
        self.added_mcp: list[tuple[str, dict]] = []
        self.removed_mcp: list[str] = []
        self.connected_mcp: list[str] = []

    async def list_skills(self):
        return list(self.installed)

    async def list_mcp_servers(self):
        return []

    async def get_skill(self, name: str):
        return next(
            item
            for item in self.installed
            if item.get("name") == name or item.get("install_dir") == name
        )

    async def download_skill_archive(self, name: str):
        return b"PK\x03\x04live-snapshot"

    async def add_mcp_server(self, *, name: str, config: dict):
        self.added_mcp.append((name, config))

    async def connect_mcp(self, name: str):
        self.connected_mcp.append(name)

    async def remove_mcp_server(self, name: str):
        self.added_mcp = [item for item in self.added_mcp if item[0] != name]
        self.removed_mcp.append(name)
        return {"ok": True, "name": name}

    async def install_skill(self, *, url=None, name=None, content=None):
        self.installs.append({"url": url, "name": name, "content": content})
        install_dir = name or "installed"
        self.installed.append(
            {"name": install_dir, "install_dir": install_dir, "source": "container"}
        )
        return {"name": install_dir, "install_dir": install_dir}

    async def upload_skill_archive(self, data: bytes, filename: str, name: str):
        self.uploads.append((data, filename, name))
        self.installed.append({"name": name, "install_dir": name, "source": "container"})
        return {"name": name, "install_dir": name, "skills_count": 1}

    async def uninstall_skill(self, name: str):
        self.uninstalls.append(name)
        self.installed = [
            item
            for item in self.installed
            if item.get("install_dir") != name and item.get("name") != name
        ]
        return {"ok": True, "name": name}


@pytest.fixture
async def clean_overrides():
    """Catalogue shelf decisions are global rows; do not leak them between tests."""
    yield
    from sqlalchemy import delete

    from db.models.catalog_override import CatalogOverride

    async with get_db_session() as session:
        await session.execute(delete(CatalogOverride))


@pytest.fixture
async def skill_api_users():
    suffix = uuid.uuid4().hex[:10]
    owner_id = f"skill_api_owner_{suffix}"
    buyer_id = f"skill_api_buyer_{suffix}"
    repo = PgUserRepo()
    await repo.create(id=owner_id, username=f"owner-{suffix}", password_hash="unused")
    await repo.create(id=buyer_id, username=f"buyer-{suffix}", password_hash="unused")
    return owner_id, buyer_id, suffix


@pytest.mark.asyncio
async def test_published_skill_appears_installs_and_keeps_buyer_provenance(
    monkeypatch, skill_api_users
):
    owner_id, buyer_id, suffix = skill_api_users
    slug = f"shared-greeting-{suffix}"
    archive = b"PK\x03\x04safe-community-snapshot"
    created = await upsert_personal_snapshot(
        owner_id,
        {
            "name": slug,
            "install_dir": slug,
            "description": "Return one approved greeting.",
            "icon": "👋",
            "requires_mcp": [],
            "files": [],
        },
        archive,
    )

    buyer_sandbox = FakeSkillSandbox()

    async def client_for(*, user_id: str):
        return buyer_sandbox if user_id == buyer_id else None

    from sandbox.manager import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "get_client_any", client_for)

    before = await metadata.get_catalog(current_user={"user_id": buyer_id})
    assert all(item["id"] != f"community:{created['id']}" for item in before["skills"])

    published = await publish_personal_skill(owner_id, slug)
    catalog_id = f"community:{published['id']}"
    visible = await metadata.get_catalog(current_user={"user_id": buyer_id})
    item = next(entry for entry in visible["skills"] if entry["id"] == catalog_id)
    assert item["community"] is True
    assert item["installed"] is False

    monkeypatch.setattr(
        "skill.catalog.catalog_index",
        lambda: {
            "mcp:memory-dep": {
                "id": "memory-dep",
                "kind": "mcp",
                "name": "memory",
                "config": {"type": "stdio", "command": "memory-server"},
            }
        },
    )
    result = await metadata.install_from_catalog(
        metadata.InstallCatalogBody(
            id=catalog_id,
            kind="skill",
            with_mcp=["memory-dep"],
            env={"memory-dep": {"MEMORY_TOKEN": "configured-outside-skill"}},
        ),
        current_user={"user_id": buyer_id},
    )
    assert [item["kind"] for item in result["installed"]] == ["mcp", "skill"]
    assert result["installed"][-1]["status"] == "installed"
    assert buyer_sandbox.added_mcp == [
        (
            "memory",
            {
                "type": "stdio",
                "command": "memory-server",
                "env": {"MEMORY_TOKEN": "configured-outside-skill"},
            },
        )
    ]
    assert buyer_sandbox.connected_mcp == ["memory"]
    assert buyer_sandbox.uploads == [(archive, f"{slug}.zip", slug)]

    # Repeating the same store install is idempotent when both the live target
    # and this user's exact catalogue provenance already exist.
    again = await metadata.install_from_catalog(
        metadata.InstallCatalogBody(id=catalog_id, kind="skill"),
        current_user={"user_id": buyer_id},
    )
    assert again["installed"][-1]["status"] == "installed"
    assert buyer_sandbox.uploads == [(archive, f"{slug}.zip", slug)]

    listed = await metadata.list_skills(current_user={"user_id": buyer_id})
    installed = next(entry for entry in listed if entry.get("name") == slug)
    assert installed["category"] == "store"
    assert installed["catalog_id"] == catalog_id

    # Store installation provenance is not authorship. Direct API calls may
    # not re-publish somebody else's package under the buyer's account.
    with pytest.raises(HTTPException) as claim:
        await metadata.publish_skill(slug, current_user={"user_id": buyer_id})
    assert claim.value.status_code == 404


@pytest.mark.asyncio
async def test_private_skill_download_is_owner_only(monkeypatch, skill_api_users):
    owner_id, buyer_id, suffix = skill_api_users
    slug = f"private-greeting-{suffix}"
    archive = b"PK\x03\x04private-snapshot"
    await upsert_personal_snapshot(
        owner_id,
        {"name": slug, "install_dir": slug, "description": "Private", "files": []},
        archive,
    )

    from sandbox.manager import sandbox_manager

    async def no_client(*, user_id: str):
        return None

    monkeypatch.setattr(sandbox_manager, "get_client_any", no_client)
    listed = await metadata.list_skills(current_user={"user_id": owner_id})
    library_entry = next(item for item in listed if item.get("library_id"))
    assert library_entry["name"] == slug
    assert library_entry["category"] == "personal"
    assert library_entry["source"] == "library"

    response = await metadata.download_skill(slug, current_user={"user_id": owner_id})
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert body == archive
    assert response.media_type == "application/zip"
    assert f'{slug}.zip' in response.headers["content-disposition"]

    with pytest.raises(HTTPException) as error:
        await metadata.download_skill(slug, current_user={"user_id": buyer_id})
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_missing_personal_skill_restores_only_to_its_owner_sandbox(
    monkeypatch, skill_api_users
):
    owner_id, buyer_id, suffix = skill_api_users
    slug = f"restore-personal-{suffix}"
    archive = b"PK\x03\x04durable-restore"
    created = await upsert_personal_snapshot(
        owner_id,
        {
            "name": slug,
            "install_dir": slug,
            "description": "Restore me after compute is replaced.",
            "files": [],
        },
        archive,
    )
    owner_sandbox = FakeSkillSandbox()
    buyer_sandbox = FakeSkillSandbox()

    async def client_for(*, user_id: str):
        return owner_sandbox if user_id == owner_id else buyer_sandbox

    from sandbox.manager import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "get_client_any", client_for)

    restored = await metadata.list_skills(current_user={"user_id": owner_id})
    personal = next(item for item in restored if item.get("library_id") == created["id"])
    assert personal["category"] == "personal"
    assert personal["source"] == "container"
    assert owner_sandbox.uploads == [(archive, f"{slug}.zip", slug)]

    # A refresh sees the live copy and must not upload the durable ZIP again.
    await metadata.list_skills(current_user={"user_id": owner_id})
    assert owner_sandbox.uploads == [(archive, f"{slug}.zip", slug)]

    buyer_view = await metadata.list_skills(current_user={"user_id": buyer_id})
    assert all(item.get("name") != slug for item in buyer_view)
    assert buyer_sandbox.uploads == []


@pytest.mark.asyncio
async def test_community_install_conflict_and_provenance_failure_roll_back_exact_package(
    monkeypatch, skill_api_users
):
    owner_id, buyer_id, suffix = skill_api_users
    slug = f"rollback-community-{suffix}"
    created = await upsert_personal_snapshot(
        owner_id,
        {"name": slug, "install_dir": slug, "description": "Rollback", "files": []},
        b"PK\x03\x04rollback-snapshot",
    )
    published = await publish_personal_skill(owner_id, created["id"])
    catalog_id = f"community:{published['id']}"
    sandbox = FakeSkillSandbox(
        installed=[{"name": slug, "install_dir": slug, "source": "container"}]
    )

    async def client_for(*, user_id: str):
        return sandbox

    from sandbox.manager import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "get_client_any", client_for)

    # The same path without matching store provenance is a real conflict, not
    # an idempotent success and never an overwrite.
    with pytest.raises(HTTPException) as conflict:
        await metadata.install_from_catalog(
            metadata.InstallCatalogBody(id=catalog_id, kind="skill"),
            current_user={"user_id": buyer_id},
        )
    assert conflict.value.status_code == 409
    assert sandbox.uploads == []

    sandbox.installed.clear()

    async def fail_record(**kwargs):
        raise RuntimeError("provenance database unavailable")

    monkeypatch.setattr("skill.user_library.record_store_installation", fail_record)
    with pytest.raises(HTTPException) as failed:
        await metadata.install_from_catalog(
            metadata.InstallCatalogBody(id=catalog_id, kind="skill"),
            current_user={"user_id": buyer_id},
        )
    assert failed.value.status_code == 500
    assert "provenance" in failed.value.detail
    assert "rolled back" in failed.value.detail
    assert sandbox.uninstalls == [slug]
    assert sandbox.installed == []


@pytest.mark.asyncio
async def test_uninstall_deletes_personal_owner_row_but_only_store_provenance(
    monkeypatch, skill_api_users
):
    owner_id, buyer_id, suffix = skill_api_users
    personal_slug = f"delete-personal-{suffix}"
    personal = await upsert_personal_snapshot(
        owner_id,
        {
            "name": personal_slug,
            "install_dir": personal_slug,
            "description": "Owner draft",
            "files": [],
        },
        b"PK\x03\x04owner-draft",
    )
    personal_sandbox = FakeSkillSandbox(
        installed=[
            {
                "name": personal_slug,
                "install_dir": personal_slug,
                "source": "container",
            }
        ]
    )

    store_slug = f"delete-store-{suffix}"
    store_owner = await upsert_personal_snapshot(
        owner_id,
        {
            "name": store_slug,
            "install_dir": store_slug,
            "description": "Published package",
            "files": [],
        },
        b"PK\x03\x04published-package",
    )
    published = await publish_personal_skill(owner_id, store_owner["id"])
    await record_store_installation(
        user_id=buyer_id,
        catalog_id=f"community:{published['id']}",
        name=store_slug,
        install_dir=store_slug,
    )
    store_sandbox = FakeSkillSandbox(
        installed=[
            {"name": store_slug, "install_dir": store_slug, "source": "container"}
        ]
    )

    async def client_for(*, user_id: str):
        return personal_sandbox if user_id == owner_id else store_sandbox

    from sandbox.manager import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "get_client_any", client_for)

    await metadata.uninstall_skill(personal_slug, current_user={"user_id": owner_id})
    assert personal_sandbox.uninstalls == [personal_slug]
    assert await get_owned_skill(owner_id, personal["id"]) is None

    await metadata.uninstall_skill(store_slug, current_user={"user_id": buyer_id})
    assert store_sandbox.uninstalls == [store_slug]
    assert await get_owned_skill(owner_id, published["id"]) is not None
    hypothetical_live_copy = await annotate_installed_skills(
        buyer_id,
        [{"name": store_slug, "install_dir": store_slug, "source": "container"}],
    )
    assert hypothetical_live_copy[0]["category"] == "installed"


async def _installs(user_id: str) -> dict[str, str]:
    """Recorded store provenance for one user, keyed by catalogue id."""
    async with get_db_session() as session:
        rows = (await session.execute(
            select(SkillInstall).where(SkillInstall.user_id == user_id)
        )).scalars()
        return {row.catalog_id: row.kind for row in rows}


@pytest.mark.asyncio
async def test_catalogue_install_records_provenance_and_uninstall_clears_it(
    monkeypatch, skill_api_users
):
    _, buyer_id, _ = skill_api_users
    sandbox = FakeSkillSandbox()

    async def client_for(*, user_id: str):
        return sandbox

    from sandbox.manager import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "get_client_any", client_for)

    shelf = {
        entry["catalog_id"]: entry
        for entry in (await metadata.get_catalog(
            current_user={"user_id": buyer_id}
        ))["skills"]
    }
    # Q3: the Anthropic pack ships off the shelf, so the store does not show it.
    assert "skill:anthropic-skills" not in shelf
    assert shelf["skill:web-research"]["origin"] == "official"
    assert shelf["skill:web-research"]["official"] is True
    assert shelf["skill:web-research"]["installs_count"] == 0

    result = await metadata.install_from_catalog(
        metadata.InstallCatalogBody(
            id="web-research", kind="skill", with_mcp=["firecrawl"],
        ),
        current_user={"user_id": buyer_id},
    )
    assert [item["kind"] for item in result["installed"]] == ["mcp", "skill"]
    # The key is the catalogue entry's, not a community row's (AC-9).
    assert await _installs(buyer_id) == {
        "mcp:firecrawl": "mcp", "skill:web-research": "skill",
    }

    listed = await metadata.list_skills(current_user={"user_id": buyer_id})
    entry = next(item for item in listed if item.get("name") == "web-research")
    assert entry["category"] == "store"
    assert entry["catalog_id"] == "skill:web-research"

    counted = await metadata.get_catalog(current_user={"user_id": buyer_id})
    installed = next(
        item for item in counted["skills"] if item["catalog_id"] == "skill:web-research"
    )
    assert installed["installs_count"] == 1

    await metadata.uninstall_skill("web-research", current_user={"user_id": buyer_id})
    assert await _installs(buyer_id) == {"mcp:firecrawl": "mcp"}

    await metadata.remove_mcp_server("firecrawl", current_user={"user_id": buyer_id})
    assert sandbox.removed_mcp == ["firecrawl"]
    assert await _installs(buyer_id) == {}


@pytest.mark.asyncio
async def test_catalogue_install_rolls_back_when_provenance_fails(
    monkeypatch, skill_api_users
):
    _, buyer_id, _ = skill_api_users
    sandbox = FakeSkillSandbox()

    async def client_for(*, user_id: str):
        return sandbox

    from sandbox.manager import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "get_client_any", client_for)

    async def fail_record(**kwargs):
        raise ValueError("catalog_id disagrees with kind")

    monkeypatch.setattr("skill.user_library.record_store_installation", fail_record)
    with pytest.raises(HTTPException) as failed:
        await metadata.install_from_catalog(
            metadata.InstallCatalogBody(id="web-research", kind="skill"),
            current_user={"user_id": buyer_id},
        )
    assert failed.value.status_code == 500
    assert "provenance" in failed.value.detail
    assert "rolled back" in failed.value.detail
    assert sandbox.uninstalls == ["web-research"]
    assert await _installs(buyer_id) == {}


@pytest.mark.asyncio
async def test_delisted_catalogue_entries_cannot_be_installed_by_id(
    monkeypatch, skill_api_users, clean_overrides
):
    """Q4/AC-10: delisting withholds an entry, it does not merely hide it.

    The store filters what it shows, but a catalog_id is readable from a stale
    store response or an install-count badge, so the install endpoint has to
    apply the same shelf — otherwise an operator's decision costs a determined
    caller one hand-written request.
    """
    _, buyer_id, _ = skill_api_users
    sandbox = FakeSkillSandbox()

    async def client_for(*, user_id: str):
        return sandbox

    from sandbox.manager import sandbox_manager
    from skill.user_library import set_listing

    monkeypatch.setattr(sandbox_manager, "get_client_any", client_for)

    # A code default: the Anthropic pack ships delisted.
    with pytest.raises(HTTPException) as off_shelf:
        await metadata.install_from_catalog(
            metadata.InstallCatalogBody(id="anthropic-skills", kind="skill"),
            current_user={"user_id": buyer_id},
        )
    assert off_shelf.value.status_code == 409
    assert sandbox.installs == []
    assert await _installs(buyer_id) == {}

    # An operator decision, on a skill and on a server.
    await set_listing("skill:web-research", "delisted", note="malware", actor_user_id="op")
    await set_listing("mcp:playwright", "delisted", note="unmaintained", actor_user_id="op")
    for entry_id, kind in (("web-research", "skill"), ("playwright", "mcp")):
        with pytest.raises(HTTPException) as blocked:
            await metadata.install_from_catalog(
                metadata.InstallCatalogBody(id=entry_id, kind=kind),
                current_user={"user_id": buyer_id},
            )
        assert blocked.value.status_code == 409
    assert sandbox.installs == []
    assert sandbox.added_mcp == []
    assert await _installs(buyer_id) == {}

    # An unknown id is still a 404, not a shelf decision.
    with pytest.raises(HTTPException) as unknown:
        await metadata.install_from_catalog(
            metadata.InstallCatalogBody(id="no-such-entry", kind="skill"),
            current_user={"user_id": buyer_id},
        )
    assert unknown.value.status_code == 404

    # Relisting restores it without any other change.
    await set_listing("skill:web-research", "listed", actor_user_id="op")
    result = await metadata.install_from_catalog(
        metadata.InstallCatalogBody(id="web-research", kind="skill"),
        current_user={"user_id": buyer_id},
    )
    assert [item["id"] for item in result["installed"]] == ["web-research"]
    assert await _installs(buyer_id) == {"skill:web-research": "skill"}


@pytest.mark.asyncio
async def test_delisted_mcp_still_installs_as_a_declared_dependency(
    monkeypatch, skill_api_users, clean_overrides
):
    """§4.4's one carve-out: delisting a server must not break its skills."""
    _, buyer_id, _ = skill_api_users
    sandbox = FakeSkillSandbox()

    async def client_for(*, user_id: str):
        return sandbox

    from sandbox.manager import sandbox_manager
    from skill.user_library import set_listing

    monkeypatch.setattr(sandbox_manager, "get_client_any", client_for)
    await set_listing("mcp:firecrawl", "delisted", note="rate limits", actor_user_id="op")

    result = await metadata.install_from_catalog(
        metadata.InstallCatalogBody(
            id="web-research", kind="skill", with_mcp=["firecrawl"],
        ),
        current_user={"user_id": buyer_id},
    )
    assert [item["kind"] for item in result["installed"]] == ["mcp", "skill"]
    assert await _installs(buyer_id) == {
        "mcp:firecrawl": "mcp", "skill:web-research": "skill",
    }


@pytest.mark.asyncio
async def test_publish_listing_follows_the_review_switch_and_the_publisher(
    monkeypatch, skill_api_users
):
    owner_id, _, suffix = skill_api_users
    config = get_config()
    slug = f"reviewed-{suffix}"
    sandbox = FakeSkillSandbox(
        installed=[{"name": slug, "install_dir": slug, "source": "container"}]
    )

    async def client_for(*, user_id: str):
        return sandbox

    from sandbox.manager import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "get_client_any", client_for)
    await upsert_personal_snapshot(
        owner_id,
        {"name": slug, "install_dir": slug, "description": "Queue me", "files": []},
        b"PK\x03\x04queued-snapshot",
    )

    admin_id = f"skill_api_admin_{suffix}"
    await PgUserRepo().create(
        id=admin_id, username=f"admin-{suffix}", password_hash="unused", role="admin",
    )

    monkeypatch.setattr(config, "skill_store_review", True)
    queued = await metadata.publish_skill(slug, current_user={"user_id": owner_id})
    assert queued["listing"] == "pending"
    assert queued["is_official"] is False
    # Nobody sees a queued submission until somebody looks at it, so every
    # admin is told there is something to look at.
    async with get_db_session() as session:
        pending = list((await session.execute(
            select(Notification).where(
                Notification.user_id == admin_id, Notification.kind == "skill_pending",
            )
        )).scalars())
    assert len(pending) == 1
    assert slug in pending[0].body

    monkeypatch.setattr(config, "skill_store_review", False)
    direct = await metadata.publish_skill(slug, current_user={"user_id": owner_id})
    assert direct["listing"] == "listed"
    assert direct["is_official"] is False

    monkeypatch.setattr(config, "skill_store_review", True)
    official = await metadata.publish_skill(
        slug, current_user={"user_id": owner_id, "role": "admin"},
    )
    # An admin publishing is the store's own editorial act: no queue, and the
    # entry lands on the official shelf.
    assert official["listing"] == "listed"
    assert official["is_official"] is True


@pytest.mark.asyncio
async def test_withdraw_is_author_only_and_takes_the_entry_off_the_shelf(
    monkeypatch, skill_api_users
):
    owner_id, buyer_id, suffix = skill_api_users
    slug = f"withdrawn-{suffix}"
    sandbox = FakeSkillSandbox(
        installed=[{"name": slug, "install_dir": slug, "source": "container"}]
    )

    async def client_for(*, user_id: str):
        return sandbox

    from sandbox.manager import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "get_client_any", client_for)
    created = await upsert_personal_snapshot(
        owner_id,
        {"name": slug, "install_dir": slug, "description": "Take me back", "files": []},
        b"PK\x03\x04withdrawable",
    )
    published = await publish_personal_skill(owner_id, created["id"])
    catalog_id = f"community:{published['id']}"
    listed = await metadata.get_catalog(current_user={"user_id": buyer_id})
    assert catalog_id in {entry["id"] for entry in listed["skills"]}

    # Owning an installed copy is not owning the release.
    with pytest.raises(HTTPException) as stranger:
        await metadata.withdraw_skill(slug, current_user={"user_id": buyer_id})
    assert stranger.value.status_code == 404

    withdrawn = await metadata.withdraw_skill(slug, current_user={"user_id": owner_id})
    assert withdrawn["publication_status"] == "withdrawn"
    gone = await metadata.get_catalog(current_user={"user_id": buyer_id})
    assert catalog_id not in {entry["id"] for entry in gone["skills"]}
    # Idempotent: withdrawing twice is not an error.
    assert (await metadata.withdraw_skill(
        slug, current_user={"user_id": owner_id},
    ))["publication_status"] == "withdrawn"

    draft_slug = f"draft-{suffix}"
    await upsert_personal_snapshot(
        owner_id,
        {"name": draft_slug, "install_dir": draft_slug, "description": "", "files": []},
        b"PK\x03\x04never-published",
    )
    with pytest.raises(HTTPException) as never:
        await metadata.withdraw_skill(draft_slug, current_user={"user_id": owner_id})
    assert never.value.status_code == 400
