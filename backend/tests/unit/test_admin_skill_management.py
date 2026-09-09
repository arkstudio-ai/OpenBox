"""CRUD, hostile ZIPs and exact-desktop operations. No real desktop is touched."""

import io
import stat
import uuid
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

from api.admin_skills import router
from api import admin_skill_desktops as desktops
from auth.middleware import get_current_user
from db.base import get_db_session
from db.models.audit_log import AuditLog
from db.models.catalog_override import CatalogOverride
from db.models.skill_catalog_package import SkillCatalogPackage
from db.models.workspace import Workspace
from db.repository.user_repo import PgUserRepo
from sandbox.client import user_scope_for
from skill import catalog_admin, user_library
from skill.catalog import shelf_index
from skill.package_validation import validate_zip, zip_manifest

BASE = "/api/admin/skills"


def manifest(name, body="A benign instruction."):
    return (
        f"---\nname: {name}\ndescription: A regression test package.\n---\n\n{body}\n"
    )


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, data in files:
            package.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
async def operator():
    suffix = uuid.uuid4().hex[:10]
    actor = await PgUserRepo().create(
        id=f"manage-{suffix}",
        username=f"manage-{suffix}",
        password_hash="unused",
        role="admin",
    )
    app = FastAPI()
    app.include_router(router)
    identity = {"user_id": actor["id"], "role": "admin"}
    app.dependency_overrides[get_current_user] = lambda: identity
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(
            client=client, id=actor["id"], name=f"managed-{suffix}", identity=identity
        )
    async with get_db_session() as session:
        await session.execute(delete(SkillCatalogPackage))
        await session.execute(delete(CatalogOverride))


async def create(operator, **extra):
    return await operator.client.post(
        f"{BASE}/store",
        json={
            "name": operator.name,
            "title": "Test skill",
            "content": manifest(operator.name),
            **extra,
        },
    )


async def test_crud_soft_delete_restore_and_optimistic_lock(operator):
    response = await create(operator)
    assert response.status_code == 200, response.text
    key = response.json()["catalog_id"]
    assert (await shelf_index())[key]["listing"] == "delisted"
    edit = await operator.client.patch(
        f"{BASE}/store/{key}",
        json={
            "title": "Edited",
            "icon": "https://images.example.test/icon.png",
            "expected_revision": 1,
        },
    )
    assert edit.status_code == 200
    detail = (await operator.client.get(f"{BASE}/store/{key}")).json()
    assert detail["title"] == "Edited" and detail["revision"] == 2
    assert detail["content"] == manifest(operator.name)
    stale = await operator.client.patch(
        f"{BASE}/store/{key}", json={"title": "Stale", "expected_revision": 1}
    )
    assert stale.status_code == 409
    assert (await create(operator)).status_code == 409
    deleted = await operator.client.post(
        f"{BASE}/store/batch-delete",
        json={
            "catalog_ids": [key, key, "skill:does-not-exist"],
            "reason": "test cleanup",
        },
    )
    assert [item["ok"] for item in deleted.json()["items"]] == [True, False]
    assert key not in await shelf_index()
    assert (await operator.client.get(f"{BASE}/store/{key}")).status_code == 404
    trash = (await operator.client.get(f"{BASE}/store?deleted=true")).json()["items"]
    assert any(item["catalog_id"] == key for item in trash)
    assert (await create(operator)).status_code == 409
    assert (
        await operator.client.post(f"{BASE}/store/{key}/restore")
    ).status_code == 200
    restored = (await operator.client.get(f"{BASE}/store/{key}")).json()
    assert restored["revision"] == 4 and restored["listing"] == "delisted"
    assert restored["content"] == manifest(operator.name)
    async with get_db_session() as session:
        audits = list(
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.resource_id == key)
                )
            ).scalars()
        )
        assert {row.action for row in audits} >= {
            "admin.skill.create",
            "admin.skill.edit",
            "admin.skill.delete",
            "admin.skill.restore",
        }
        assert all("A benign instruction" not in str(row.details) for row in audits)


async def test_builtin_edit_and_delete_persist_without_changing_code_defaults(operator):
    key = "skill:browser-qa"
    original = (await shelf_index())[key]["title"]
    response = await operator.client.patch(
        f"{BASE}/store/{key}",
        json={"title": "QA edited", "icon": "🧪", "expected_revision": 0},
    )
    assert response.status_code == 200, response.text
    assert (await shelf_index())[key]["title"] == "QA edited"
    await operator.client.post(
        f"{BASE}/store/batch-delete", json={"catalog_ids": [key], "reason": "test"}
    )
    assert key not in await shelf_index()
    await operator.client.post(f"{BASE}/store/{key}/restore")
    assert (await shelf_index())[key]["listing"] == "delisted"
    from skill.catalog import SKILL_CATALOG

    assert (
        next(e for e in SKILL_CATALOG if e["id"] == "browser-qa")["title"] == original
    )


async def test_batch_zip_mixed_results_and_content_edit_preserves_other_files(operator):
    data = archive(
        [
            (f"{operator.name}/SKILL.md", manifest(operator.name)),
            (f"{operator.name}/notes.txt", "preserve me"),
        ]
    )
    response = await operator.client.post(
        f"{BASE}/store/upload",
        files=[
            ("files", ("valid.zip", data, "application/zip")),
            ("files", ("corrupt.zip", b"not zip", "application/zip")),
            ("files", ("duplicate.zip", data, "application/zip")),
            ("files", ("wrong.tar", data, "application/zip")),
        ],
    )
    assert response.status_code == 200, response.text
    assert [i["ok"] for i in response.json()["items"]] == [True, False, False, False]
    assert response.json()["items"][2]["status"] == 409
    key = response.json()["items"][0]["catalog_id"]
    changed = manifest(operator.name, "Updated instructions.")
    edit = await operator.client.patch(
        f"{BASE}/store/{key}", json={"content": changed, "expected_revision": 1}
    )
    assert edit.status_code == 200, edit.text
    with zipfile.ZipFile(io.BytesIO(await catalog_admin.archive_bytes(key))) as package:
        assert package.read(f"{operator.name}/notes.txt") == b"preserve me"
        assert package.read(f"{operator.name}/SKILL.md").decode() == changed
    invalid = await operator.client.patch(
        f"{BASE}/store/{key}",
        json={"content": manifest("renamed"), "expected_revision": 2},
    )
    assert invalid.status_code == 422


async def test_batch_count_limit_malformed_yaml_and_empty_reason(operator):
    response = await operator.client.post(
        f"{BASE}/store/upload", files=[("files", ("x.zip", b"x"))] * 21
    )
    assert response.status_code == 422
    bad = archive([("SKILL.md", "---\nname: [unterminated\n---\nbody")])
    response = await operator.client.post(
        f"{BASE}/store/upload", files=[("files", ("x.zip", bad))]
    )
    assert response.status_code == 200 and response.json()["items"][0]["status"] == 422
    assert (
        await operator.client.post(
            f"{BASE}/store/batch-delete",
            json={"catalog_ids": ["skill:browser-qa"], "reason": "   "},
        )
    ).status_code == 422


async def test_finder_zip_metadata_is_cleaned_before_store_install(operator):
    data = archive(
        [
            (f"{operator.name}/SKILL.md", manifest(operator.name)),
            (f"__MACOSX/{operator.name}/._SKILL.md", "Finder attributes"),
            (f"{operator.name}/.DS_Store", "Finder display"),
        ]
    )
    response = await operator.client.post(
        f"{BASE}/store/upload", files=[("files", ("finder.zip", data))]
    )
    assert response.json()["items"][0]["ok"]
    stored = await catalog_admin.archive_bytes("skill:" + operator.name)
    with zipfile.ZipFile(io.BytesIO(stored)) as package:
        assert package.namelist() == [f"{operator.name}/SKILL.md"]


@pytest.mark.parametrize(
    "icon",
    [
        "javascript:alert(1)",
        "data:image/svg+xml,<svg/>",
        "http://example.test/icon.png",
        "https://user:pass@example.test/x",
        "<img onerror=x>",
    ],
)
async def test_unsafe_icon_rejected(operator, icon):
    assert (await create(operator, icon=icon)).status_code == 422


@pytest.mark.parametrize(
    "members",
    [
        [("../SKILL.md", manifest("safe"))],
        [("/SKILL.md", manifest("safe"))],
        [("a\\SKILL.md", manifest("safe"))],
        [("C:/SKILL.md", manifest("safe"))],
        [("SKILL.md", manifest("safe")), ("skill.md", "duplicate")],
        [("a/SKILL.md", manifest("safe")), ("outside.txt", "bad root")],
        [("a/b/SKILL.md", manifest("safe"))],
        [("a/SKILL.md", manifest("safe")), ("b/SKILL.md", manifest("other"))],
        [("README.md", "no manifest")],
    ],
)
def test_hostile_zip_refused(members):
    with pytest.raises(ValueError):
        validate_zip(archive(members))


def test_symlinks_and_expansion_limits(monkeypatch):
    link = zipfile.ZipInfo("evil")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError):
        validate_zip(archive([("SKILL.md", manifest("safe")), (link, "/etc/passwd")]))
    monkeypatch.setattr("skill.package_validation.MAX_EXPANDED_BYTES", 10)
    with pytest.raises(ValueError, match="Expanded"):
        validate_zip(zip_manifest("safe", manifest("safe")))


async def test_mcp_create_edit_and_no_config_in_listing(operator):
    response = await create(
        operator,
        kind="mcp",
        config={"type": "remote", "url": "https://example.test/mcp"},
    )
    assert response.status_code == 200, response.text
    key = response.json()["catalog_id"]
    assert (
        await operator.client.patch(
            f"{BASE}/store/{key}",
            json={
                "config": {
                    "type": "stdio",
                    "command": "test-command",
                    "env": {"secret": "test-only"},
                },
                "expected_revision": 1,
            },
        )
    ).status_code == 200
    rows = (
        await operator.client.get(f"{BASE}/store", params={"q": operator.name})
    ).json()["items"]
    assert len(rows) == 1 and "config" not in rows[0] and "install" not in rows[0]


async def test_community_edit_does_not_mutate_authors_archive_and_delete_hides_install(
    operator,
):
    original = zip_manifest(operator.name, manifest(operator.name))
    owned = await user_library.upsert_personal_snapshot(
        operator.id, {"name": operator.name, "install_dir": operator.name}, original
    )
    published = await user_library.publish_personal_skill(
        operator.id, owned["id"], review_required=False
    )
    key = "community:" + published["id"]
    updated = manifest(operator.name, "Operator edition")
    response = await operator.client.patch(
        f"{BASE}/store/{key}",
        json={"title": "Operator title", "content": updated, "expected_revision": 0},
    )
    assert response.status_code == 200, response.text
    release = await user_library.get_published_skill(key, include_archive=True)
    assert release["title"] == "Operator title" and release["archive_data"] != original
    own = await user_library.get_owned_skill(
        operator.id, owned["id"], include_archive=True
    )
    assert own["archive_data"] == original
    assert (
        await operator.client.get(f"{BASE}/store", params={"q": "Operator title"})
    ).json()["items"][0]["catalog_id"] == key
    await operator.client.post(
        f"{BASE}/store/batch-delete", json={"catalog_ids": [key], "reason": "test"}
    )
    assert await user_library.get_published_skill(key) is None
    assert (
        await user_library.get_owned_skill(
            operator.id, owned["id"], include_archive=True
        )
    )["archive_data"] == original
    await operator.client.post(f"{BASE}/store/{key}/restore")
    assert await user_library.get_published_skill(key) is None  # restored but hidden
    assert (await user_library.get_published_skill(key, require_listed=False))[
        "listing"
    ] == "delisted"


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/store/skill:browser-qa", None),
        ("POST", "/store", {"name": "x"}),
        ("PATCH", "/store/skill:browser-qa", {"expected_revision": 0}),
        ("POST", "/store/batch-delete", {"catalog_ids": ["skill:x"], "reason": "x"}),
        ("POST", "/store/skill:x/restore", None),
        ("GET", "/desktops", None),
        ("GET", "/desktops/d/skills?user_id=x", None),
        (
            "POST",
            "/desktops/d/uninstall",
            {"user_id": "x", "kind": "skill", "install_dir": "x", "reason": "x"},
        ),
    ],
)
async def test_all_new_routes_require_admin(operator, method, path, body):
    operator.identity["role"] = "user"
    assert (
        await operator.client.request(method, BASE + path, json=body)
    ).status_code == 403


@pytest.fixture
async def desktop(operator, monkeypatch):
    now = datetime.now(timezone.utc)
    workspace = "ws-" + operator.name
    async with get_db_session() as session:
        session.add(
            Workspace(
                id=workspace,
                name="Test workspace",
                owner_user_id=operator.id,
                created_at=now,
                updated_at=now,
            )
        )
    row = {
        "desktop_id": "desktop-test",
        "pool_state": "assigned",
        "status": "running",
        "workspace_id": workspace,
        "user_id": operator.id,
    }
    monkeypatch.setattr(
        desktops.cloud_desktop_repo, "get_by_desktop_id", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        desktops,
        "route_for_record",
        Mock(return_value=("127.0.0.1", 1234, "test-channel-key")),
    )
    client = SimpleNamespace(
        list_skills=AsyncMock(return_value=[{"name": "manual", "source": "container"}]),
        list_mcp_servers=AsyncMock(return_value=[]),
        uninstall_skill=AsyncMock(return_value={"ok": True}),
        remove_mcp_server=AsyncMock(return_value={"ok": True}),
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr(desktops, "SandboxClient", factory)
    return SimpleNamespace(row=row, client=client, factory=factory, workspace=workspace)


async def test_scan_exact_user_namespace_and_partial_failure(operator, desktop):
    desktop.client.list_mcp_servers.side_effect = RuntimeError("private error")
    response = await operator.client.get(
        f"{BASE}/desktops/desktop-test/skills", params={"user_id": operator.id}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["unavailable"] == ["mcp"] and result["items"][0]["name"] == "manual"
    args = desktop.factory.call_args.kwargs
    assert (
        args["desktop_id"] == "desktop-test"
        and args["workspace_id"] == desktop.workspace
    )
    assert args["user_scope"] == user_scope_for(operator.id)
    assert (
        "private error" not in response.text and "test-channel-key" not in response.text
    )
    desktop.client.list_skills.side_effect = RuntimeError("private error")
    assert (
        await operator.client.get(
            f"{BASE}/desktops/desktop-test/skills", params={"user_id": operator.id}
        )
    ).status_code == 503


@pytest.mark.parametrize(
    "state,code", [("stopped", 409), ("starting", 409), ("failed", 409)]
)
async def test_scan_offline_never_acquires_desktop(operator, desktop, state, code):
    desktop.row["status"] = state
    assert (
        await operator.client.get(
            f"{BASE}/desktops/desktop-test/skills", params={"user_id": operator.id}
        )
    ).status_code == code
    desktop.factory.assert_not_called()


async def test_scan_wrong_user_or_unassigned_desktop_refused(operator, desktop):
    assert (
        await operator.client.get(
            f"{BASE}/desktops/desktop-test/skills?user_id=stranger"
        )
    ).status_code == 404
    desktop.row["pool_state"] = "reserve"
    assert (
        await operator.client.get(
            f"{BASE}/desktops/desktop-test/skills", params={"user_id": operator.id}
        )
    ).status_code == 404
    desktop.factory.assert_not_called()


async def test_uninstall_verified_and_archive_kept_without_autorestore(
    operator, desktop
):
    owned = await user_library.upsert_personal_snapshot(
        operator.id,
        {"name": "manual", "install_dir": "manual"},
        zip_manifest("manual", manifest("manual")),
        desktop.workspace,
    )
    desktop.client.list_skills.side_effect = [
        [{"name": "manual", "source": "container"}],
        [],
    ]
    response = await operator.client.post(
        f"{BASE}/desktops/desktop-test/uninstall",
        json={
            "user_id": operator.id,
            "kind": "skill",
            "install_dir": "manual",
            "reason": "remove test",
        },
    )
    assert response.status_code == 200, response.text
    desktop.client.uninstall_skill.assert_awaited_once_with("manual")
    detail = await user_library.get_owned_skill(
        operator.id, owned["id"], include_archive=True, workspace_id=desktop.workspace
    )
    assert not detail["restore_available"] and detail["archive_data"]


@pytest.mark.parametrize(
    "source,dir,reason,code",
    [
        ("builtin", "manual", "test", 409),
        ("unknown", "manual", "test", 409),
        ("container", "../manual", "test", 422),
        ("container", "manual", " ", 422),
        ("container", "absent", "test", 409),
    ],
)
async def test_uninstall_guardrails(operator, desktop, source, dir, reason, code):
    desktop.client.list_skills.return_value = [{"name": "manual", "source": source}]
    response = await operator.client.post(
        f"{BASE}/desktops/desktop-test/uninstall",
        json={
            "user_id": operator.id,
            "kind": "skill",
            "install_dir": dir,
            "reason": reason,
        },
    )
    assert response.status_code == code
    desktop.client.uninstall_skill.assert_not_awaited()


async def test_uninstall_false_ack_is_not_success(operator, desktop):
    response = await operator.client.post(
        f"{BASE}/desktops/desktop-test/uninstall",
        json={
            "user_id": operator.id,
            "kind": "skill",
            "install_dir": "manual",
            "reason": "test",
        },
    )
    assert response.status_code == 502 and "still present" in response.text


async def test_timeout_is_uncertain_not_empty_or_success(operator, desktop):
    desktop.client.list_skills.side_effect = TimeoutError()
    desktop.client.list_mcp_servers.side_effect = TimeoutError()
    response = await operator.client.post(
        f"{BASE}/desktops/desktop-test/uninstall",
        json={
            "user_id": operator.id,
            "kind": "skill",
            "install_dir": "manual",
            "reason": "test",
        },
    )
    assert response.status_code == 504


async def test_upload_is_admin_only(operator):
    operator.identity["role"] = "user"
    response = await operator.client.post(
        f"{BASE}/store/upload", files=[("files", ("x.zip", b"x"))]
    )
    assert response.status_code == 403


async def test_restore_does_not_turn_display_snapshot_into_author_metadata_override(
    operator,
):
    from db.models.user_skill import UserSkill

    owned = await user_library.upsert_personal_snapshot(
        operator.id,
        {
            "name": operator.name,
            "install_dir": operator.name,
            "description": "Original",
        },
        zip_manifest(operator.name, manifest(operator.name)),
    )
    await user_library.publish_personal_skill(
        operator.id, owned["id"], review_required=False
    )
    key = "community:" + owned["id"]
    await catalog_admin.delete_entry(key, operator.id)
    async with get_db_session() as session:
        row = await session.get(UserSkill, owned["id"])
        row.published_description = "New author release"
    await catalog_admin.restore_entry(key, operator.id)
    restored = await user_library.get_published_skill(key, require_listed=False)
    assert restored["description"] == "New author release"
    assert "_operator_overlay" not in restored


async def test_max_name_audited_without_truncation(operator):
    operator.name = "a" * 64
    response = await create(operator)
    assert response.status_code == 200
    async with get_db_session() as session:
        assert await session.scalar(
            select(AuditLog.id).where(AuditLog.resource_id == "skill:" + operator.name)
        )


async def test_channel_failure_and_deleted_workspace_refuse_scan(
    operator, desktop, monkeypatch
):
    monkeypatch.setattr(
        desktops, "route_for_record", Mock(side_effect=RuntimeError("secret route"))
    )
    response = await operator.client.get(
        f"{BASE}/desktops/desktop-test/skills", params={"user_id": operator.id}
    )
    assert response.status_code == 503 and "secret route" not in response.text
    async with get_db_session() as session:
        workspace = await session.get(Workspace, desktop.workspace)
        workspace.is_deleted = True
    assert (
        await operator.client.get(
            f"{BASE}/desktops/desktop-test/skills", params={"user_id": operator.id}
        )
    ).status_code == 404
    desktop.factory.assert_not_called()


async def test_skill_collection_is_one_removable_directory(operator, desktop):
    rows = [
        {"name": name, "install_dir": "collection", "source": "container"}
        for name in ("one", "two")
    ]
    projected = desktops._project(rows, "skill")
    assert len(projected) == 1 and projected[0]["names"] == ["one", "two"]
    desktop.client.list_skills.side_effect = [rows, []]
    response = await operator.client.post(
        f"{BASE}/desktops/desktop-test/uninstall",
        json={
            "user_id": operator.id,
            "kind": "skill",
            "install_dir": "collection",
            "reason": "test",
        },
    )
    assert response.status_code == 200, response.text
    desktop.client.uninstall_skill.assert_awaited_once_with("collection")


async def test_deleted_mcp_cannot_return_as_dependency(operator, monkeypatch):
    from api import metadata
    from fastapi import HTTPException
    from sandbox.manager import sandbox_manager

    await catalog_admin.delete_entry("mcp:firecrawl", operator.id)
    acquire = AsyncMock()
    monkeypatch.setattr(sandbox_manager, "get_client_any", acquire)
    with pytest.raises(HTTPException) as failure:
        await metadata.install_from_catalog(
            metadata.InstallCatalogBody(
                id="web-research", kind="skill", with_mcp=["firecrawl"]
            ),
            current_user={"user_id": operator.id},
        )
    assert failure.value.status_code == 409
    acquire.assert_not_awaited()


async def test_managed_zip_installs_and_history_uses_display_edits(
    operator, monkeypatch
):
    from api import metadata
    from sandbox.manager import sandbox_manager
    from fastapi import HTTPException

    assert (await create(operator, icon="🚀", listing="listed")).status_code == 200
    sandbox = SimpleNamespace(
        list_skills=AsyncMock(return_value=[]),
        upload_skill_archive=AsyncMock(
            return_value={"name": operator.name, "install_dir": operator.name}
        ),
        uninstall_skill=AsyncMock(),
    )
    monkeypatch.setattr(
        sandbox_manager, "get_client_any", AsyncMock(return_value=sandbox)
    )
    result = await metadata.install_from_catalog(
        metadata.InstallCatalogBody(id=operator.name, kind="skill"),
        current_user={"user_id": operator.id},
    )
    assert result["ok"]
    data, filename, name = sandbox.upload_skill_archive.call_args.args
    assert validate_zip(data)["name"] == name == operator.name
    assert filename == operator.name + ".zip"
    history = (
        await operator.client.get(
            f"{BASE}/installs", params={"catalog_id": "skill:" + operator.name}
        )
    ).json()["items"]
    assert (
        history[0]["title"] == "Test skill"
        and history[0]["icon"] == "🚀"
        and history[0]["origin"] == "official"
    )
    sandbox.list_skills.return_value = [
        {"name": operator.name, "install_dir": operator.name}
    ]
    with pytest.raises(HTTPException) as failure:
        await metadata.install_from_catalog(
            metadata.InstallCatalogBody(id=operator.name, kind="skill"),
            current_user={"user_id": operator.id},
        )
    assert failure.value.status_code == 409
    assert sandbox.upload_skill_archive.await_count == 1


async def test_managed_catalog_carries_dependencies_for_user_store(
    operator, monkeypatch
):
    from api import metadata
    from sandbox.manager import sandbox_manager

    content = manifest(operator.name).replace(
        "description:", "requires_mcp: [firecrawl]\ndescription:"
    )
    response = await create(operator, content=content, listing="listed")
    assert response.status_code == 200
    monkeypatch.setattr(sandbox_manager, "get_client_any", AsyncMock(return_value=None))
    catalog = await metadata.get_catalog(current_user={"user_id": operator.id})
    entry = next(item for item in catalog["skills"] if item["id"] == operator.name)
    assert entry["requires_mcp"] == ["firecrawl"] and entry["missing_mcp"] == [
        "firecrawl"
    ]
    edited = await operator.client.patch(
        f"{BASE}/store/skill:{operator.name}",
        json={"content": manifest(operator.name), "expected_revision": 1},
    )
    assert edited.status_code == 200
    assert (await shelf_index())["skill:" + operator.name]["requires_mcp"] == []


@pytest.mark.parametrize(
    "config",
    [
        {"type": "stdio", "command": ["invalid"]},
        {"type": "stdio", "command": "x", "args": "not a list"},
        {"type": "stdio", "command": "x", "env": {"KEY": 42}},
        {"type": "remote", "url": "https:"},
        {"type": "remote", "url": "https://user:pass@example.test"},
        {"type": "remote", "url": "https://example.test", "timeout": -1},
    ],
)
async def test_invalid_mcp_config_rejected_on_create_and_edit(operator, config):
    assert (await create(operator, kind="mcp", config=config)).status_code == 422
    good = {"type": "stdio", "command": "test"}
    assert (await create(operator, kind="mcp", config=good)).status_code == 200
    response = await operator.client.patch(
        f"{BASE}/store/mcp:{operator.name}",
        json={"config": config, "expected_revision": 1},
    )
    assert response.status_code == 422
