"""API-level community Skill flow across two users and one immutable ZIP."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from api import metadata
from db.repository.user_repo import PgUserRepo
from skill.user_library import (
    annotate_installed_skills,
    get_owned_skill,
    publish_personal_skill,
    record_community_installation,
    upsert_personal_snapshot,
)


class FakeSkillSandbox:
    def __init__(self, *, installed: list[dict] | None = None):
        self.installed = installed or []
        self.uploads: list[tuple[bytes, str, str]] = []
        self.uninstalls: list[str] = []
        self.added_mcp: list[tuple[str, dict]] = []
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

    monkeypatch.setattr("skill.user_library.record_community_installation", fail_record)
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
    await record_community_installation(
        user_id=buyer_id,
        user_skill_id=published["id"],
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
