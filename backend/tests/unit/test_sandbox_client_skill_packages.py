"""SandboxClient contract for safe user-skill package operations."""

import json

import httpx
import pytest

from sandbox.client import ExecuteResult, SandboxClient


@pytest.mark.asyncio
async def test_skill_package_client_methods_use_the_action_server_contract(monkeypatch):
    requests: list[tuple[str, str, bytes]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, request.content))
        if request.url.path == "/skills/create":
            return httpx.Response(200, json={"name": "greeting-helper", "created": True})
        if request.url.path == "/skills/greeting-helper/archive":
            return httpx.Response(200, content=b"PK\x03\x04archive")
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
    assert archive == b"PK\x03\x04archive"
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
            return httpx.Response(200, content=b"PK\x03\x04wuying-archive")
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
    assert archive == b"PK\x03\x04wuying-archive"
    assert exported["filename"] == "wuying-helper.zip"
    assert ("GET", "/download") in requests
