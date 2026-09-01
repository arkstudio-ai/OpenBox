"""SandboxClient contract for safe user-skill package operations."""

import json
import io
import zipfile

import httpx
import pytest

from skill.archive import SkillArchiveValidationError
from sandbox.client import (
    ExecuteResult,
    SandboxClient,
    SkillArchiveAlreadyExistsError,
    SkillRestoreFencedError,
)


def _valid_skill_zip(name: str = "greeting-helper") -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            f"{name}/SKILL.md",
            f"---\nname: {name}\ndescription: d\n---\nbody\n",
        )
    return archive.getvalue()


@pytest.mark.asyncio
async def test_invalid_personal_zip_never_reaches_action_server(monkeypatch):
    def handle(_request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid archive must be rejected before the upload request")

    client = SandboxClient("sandbox", 8000, "test-key")
    transport = httpx.MockTransport(handle)

    def mock_client(timeout=30.0):
        return httpx.AsyncClient(
            base_url="http://sandbox:8000",
            transport=transport,
            timeout=timeout,
        )

    monkeypatch.setattr(client, "_client", mock_client)
    with pytest.raises(SkillArchiveValidationError):
        await client.upload_skill_archive(
            b"PK\x03\x04not-a-complete-zip",
            "broken.zip",
            "broken",
        )


@pytest.mark.asyncio
async def test_invalid_downloaded_snapshot_is_not_returned_for_persistence(monkeypatch):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/skills/broken/archive":
            return httpx.Response(200, content=b"PK\x03\x04truncated")
        return httpx.Response(404)

    client = SandboxClient("sandbox", 8000, "test-key")
    transport = httpx.MockTransport(handle)

    def mock_client(timeout=30.0):
        return httpx.AsyncClient(
            base_url="http://sandbox:8000",
            transport=transport,
            timeout=timeout,
        )

    monkeypatch.setattr(client, "_client", mock_client)
    with pytest.raises(SkillArchiveValidationError):
        await client.download_skill_archive("broken")


@pytest.mark.asyncio
async def test_skill_package_client_methods_use_the_action_server_contract(monkeypatch):
    requests: list[tuple[str, str, bytes]] = []
    expected_archive = _valid_skill_zip()

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, request.content))
        if request.url.path == "/skills/create":
            return httpx.Response(200, json={"name": "greeting-helper", "created": True})
        if request.url.path == "/skills/greeting-helper/archive":
            return httpx.Response(200, content=expected_archive)
        if request.url.path == "/skills/greeting-helper/export":
            return httpx.Response(
                200,
                json={
                    "path": "/workspace/exports/greeting-helper.zip",
                    "filename": "greeting-helper.zip",
                    "size": 11,
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handle)
    client = SandboxClient("sandbox", 8000, "test-key")

    def mock_client(timeout=30.0):
        return httpx.AsyncClient(
            base_url="http://sandbox:8000",
            headers={"X-API-Key": "test-key"},
            transport=transport,
            timeout=timeout,
        )

    monkeypatch.setattr(client, "_client", mock_client)

    created = await client.create_skill(
        "greeting-helper",
        "---\nname: greeting-helper\ndescription: d\n---\nbody\n",
        [{"path": "references/greeting.txt", "content": "hello"}],
    )
    archive = await client.download_skill_archive("greeting-helper")
    exported = await client.export_skill_archive("greeting-helper")

    assert created["created"] is True
    assert archive == expected_archive
    assert exported["path"] == "/workspace/exports/greeting-helper.zip"
    assert [(method, path) for method, path, _ in requests] == [
        ("POST", "/skills/create"),
        ("GET", "/skills/greeting-helper/archive"),
        ("POST", "/skills/greeting-helper/export"),
    ]
    assert json.loads(requests[0][2]) == {
        "name": "greeting-helper",
        "skill_md": "---\nname: greeting-helper\ndescription: d\n---\nbody\n",
        "files": [{"path": "references/greeting.txt", "content": "hello"}],
    }


@pytest.mark.asyncio
async def test_skill_package_client_falls_back_for_legacy_wuying(monkeypatch):
    requests: list[tuple[str, str]] = []
    expected_archive = _valid_skill_zip("wuying-helper")

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/skills/create":
            return httpx.Response(405)
        if request.url.path == "/skills/wuying-helper":
            return httpx.Response(
                200,
                json={
                    "name": "wuying-helper",
                    "install_dir": "wuying-helper",
                    "source": "container",
                },
            )
        if request.url.path in {
            "/skills/wuying-helper/archive",
            "/skills/wuying-helper/export",
        }:
            return httpx.Response(404)
        if request.url.path == "/download":
            return httpx.Response(200, content=expected_archive)
        return httpx.Response(404)

    transport = httpx.MockTransport(handle)
    client = SandboxClient("sandbox", 8000, "test-key")

    def mock_client(timeout=30.0):
        return httpx.AsyncClient(
            base_url="http://sandbox:8000",
            headers={"X-API-Key": "test-key"},
            transport=transport,
            timeout=timeout,
        )

    commands: list[str] = []
    writes: list[tuple[str, str]] = []

    async def execute(command, timeout=120, workdir="/workspace"):
        commands.append(command)
        return ExecuteResult(exit_code=0, stdout="", stderr="")

    async def write_file(path, content):
        writes.append((path, content))

    async def legacy_export(name):
        assert name == "wuying-helper"
        return {
            "path": "/workspace/exports/wuying-helper.zip",
            "filename": "wuying-helper.zip",
            "size": 18,
        }

    monkeypatch.setattr(client, "_client", mock_client)
    monkeypatch.setattr(client, "execute", execute)
    monkeypatch.setattr(client, "write_file", write_file)
    monkeypatch.setattr(client, "_legacy_export_skill_archive", legacy_export)

    created = await client.create_skill(
        "wuying-helper",
        "---\nname: wuying-helper\ndescription: d\n---\nbody\n",
        [{"path": "references/greeting.txt", "content": "hello"}],
    )
    archive = await client.download_skill_archive("wuying-helper")
    exported = await client.export_skill_archive("wuying-helper")

    assert created["created"] is True
    assert len(commands) == 2
    assert any(path.endswith("/SKILL.md") for path, _ in writes)
    assert any(path.endswith("/references/greeting.txt") for path, _ in writes)
    assert archive == expected_archive
    assert exported["filename"] == "wuying-helper.zip"
    assert ("GET", "/download") in requests


@pytest.mark.asyncio
async def test_create_only_archive_upload_is_capability_gated_and_maps_conflict(
    monkeypatch,
):
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/alive":
            return httpx.Response(
                200,
                json={
                    "capabilities": [
                        "skill_archive_create_only_v1",
                        "skill_restore_fence_v1",
                    ]
                },
            )
        if request.url.path == "/skills/upload":
            body = request.content
            assert b'name="create_only"' in body
            assert b"\r\n\r\ntrue\r\n" in body
            assert b'name="restore_generation"' in body
            assert b"\r\n\r\n1\r\n" in body
            return httpx.Response(
                409,
                json={
                    "detail": {
                        "code": "skill_already_exists",
                        "name": "greeting-helper",
                        "message": "Skill 'greeting-helper' already exists",
                    }
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handle)
    client = SandboxClient("sandbox", 8000, "test-key")

    def mock_client(timeout=30.0):
        return httpx.AsyncClient(
            base_url="http://sandbox:8000",
            headers={"X-API-Key": "test-key"},
            transport=transport,
            timeout=timeout,
        )

    monkeypatch.setattr(client, "_client", mock_client)
    archive = _valid_skill_zip()

    with pytest.raises(SkillArchiveAlreadyExistsError) as conflict:
        await client.upload_skill_archive(
            archive,
            "greeting-helper.zip",
            "greeting-helper",
            create_only=True,
            restore_generation=1,
        )

    assert conflict.value.install_dir == "greeting-helper"
    assert client._catalogue_epoch == 1
    assert [request.url.path for request in requests] == [
        "/alive",
        "/skills/upload",
    ]


@pytest.mark.asyncio
async def test_create_only_archive_upload_never_downgrades_on_legacy_server(
    monkeypatch,
):
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"capabilities": []})

    transport = httpx.MockTransport(handle)
    client = SandboxClient("sandbox", 8000, "test-key")

    def mock_client(timeout=30.0):
        return httpx.AsyncClient(
            base_url="http://sandbox:8000",
            headers={"X-API-Key": "test-key"},
            transport=transport,
            timeout=timeout,
        )

    monkeypatch.setattr(client, "_client", mock_client)
    archive = _valid_skill_zip()

    with pytest.raises(RuntimeError, match="create-only"):
        await client.upload_skill_archive(
            archive,
            "greeting-helper.zip",
            "greeting-helper",
            create_only=True,
        )

    assert paths == ["/alive"]


@pytest.mark.asyncio
async def test_restore_fence_conflict_and_fenced_uninstall_use_wire_generation(
    monkeypatch,
):
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/alive":
            return httpx.Response(
                200,
                json={
                    "capabilities": [
                        "skill_archive_create_only_v1",
                        "skill_restore_fence_v1",
                    ]
                },
            )
        if request.method == "DELETE":
            assert request.url.params["mutation_generation"] == "2"
            return httpx.Response(200, json={"ok": True, "mutation_generation": 2})
        return httpx.Response(
            409,
            json={
                "detail": {
                    "code": "skill_restore_fenced",
                    "name": "greeting-helper",
                    "fenced_through_generation": 2,
                }
            },
        )

    transport = httpx.MockTransport(handle)
    client = SandboxClient("sandbox", 8000, "test-key")

    def mock_client(timeout=30.0):
        return httpx.AsyncClient(
            base_url="http://sandbox:8000",
            headers={"X-API-Key": "test-key"},
            transport=transport,
            timeout=timeout,
        )

    monkeypatch.setattr(client, "_client", mock_client)
    archive = _valid_skill_zip()

    with pytest.raises(SkillRestoreFencedError) as stale:
        await client.upload_skill_archive(
            archive,
            "greeting-helper.zip",
            "greeting-helper",
            create_only=True,
            restore_generation=1,
        )
    assert stale.value.fenced_through_generation == 2

    removed = await client.uninstall_skill(
        "greeting-helper",
        mutation_generation=2,
    )
    assert removed["mutation_generation"] == 2
    assert [request.url.path for request in requests] == [
        "/alive",
        "/skills/upload",
        "/skills/greeting-helper",
    ]
