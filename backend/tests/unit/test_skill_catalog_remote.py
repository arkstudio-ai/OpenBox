"""Remote Skill-store overlays use one validated listing/install snapshot."""
from __future__ import annotations

import pytest

from api import metadata
from skill.catalog import load_catalog


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _RemoteClient:
    payload: dict = {}
    requested: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str):
        type(self).requested.append(url)
        return _Response(type(self).payload)


class _Sandbox:
    def __init__(self, installed: list[dict] | None = None):
        self.installed = installed or []
        self.installs: list[dict] = []
        self.mcp: list[tuple[str, dict]] = []
        self.connected: list[str] = []

    async def list_skills(self):
        return list(self.installed)

    async def list_mcp_servers(self):
        return []

    async def install_skill(self, *, url=None, name=None, content=None):
        self.installs.append({"url": url, "name": name, "content": content})
        return {"name": name}

    async def add_mcp_server(self, *, name: str, config: dict):
        self.mcp.append((name, config))

    async def connect_mcp(self, name: str):
        self.connected.append(name)


@pytest.fixture
def remote_catalog(monkeypatch):
    _RemoteClient.requested = []
    _RemoteClient.payload = {
        "skills": [
            {
                "id": "operator-helper",
                "kind": "skill",
                "name": "operator-helper",
                "title": "Operator helper",
                "description": "An operator-reviewed inline package.",
                "requires_mcp": ["operator-memory"],
                "install": {
                    "name": "operator-helper",
                    "content": "---\nname: operator-helper\ndescription: helper\n---\n# Helper\n",
                },
                # These fields are computed by the backend and cannot be used
                # by an overlay to impersonate a community/owner snapshot.
                "community": True,
                "catalog_id": "community:forged",
                "installed": True,
            }
        ],
        "mcp": [
            {
                "id": "operator-memory",
                "kind": "mcp",
                "name": "operator-memory",
                "title": "Operator memory",
                "config": {
                    "type": "stdio",
                    "command": "operator-memory-server",
                    "args": ["--stdio"],
                    "env": {},
                    "timeout": 30,
                },
            }
        ],
    }
    monkeypatch.setenv("OPENBOX_CATALOG_URL", "https://catalog.example.test/openbox.json")
    monkeypatch.setattr("httpx.AsyncClient", _RemoteClient)
    return _RemoteClient.payload


@pytest.mark.asyncio
async def test_remote_entry_visible_then_installs_from_same_validated_source(
    monkeypatch,
    remote_catalog,
):
    sandbox = _Sandbox()

    async def client_for(*, user_id: str):
        assert user_id == "buyer"
        return sandbox

    async def no_community_entries():
        return []

    from sandbox.manager import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "get_client_any", client_for)
    monkeypatch.setattr(
        "skill.user_library.list_published_catalog_entries",
        no_community_entries,
    )

    visible = await metadata.get_catalog(current_user={"user_id": "buyer"})
    item = next(row for row in visible["skills"] if row["id"] == "operator-helper")
    assert item["installed"] is False
    assert item["missing_mcp"] == ["operator-memory"]
    assert "community" not in item
    assert "catalog_id" not in item

    result = await metadata.install_from_catalog(
        metadata.InstallCatalogBody(
            id="operator-helper",
            kind="skill",
            with_mcp=["operator-memory"],
            env={"operator-memory": {"OPERATOR_TOKEN": "secret-at-install-time"}},
        ),
        current_user={"user_id": "buyer"},
    )

    assert [row["kind"] for row in result["installed"]] == ["mcp", "skill"]
    assert sandbox.installs == [
        {
            "url": None,
            "name": "operator-helper",
            "content": remote_catalog["skills"][0]["install"]["content"],
        }
    ]
    assert sandbox.mcp == [
        (
            "operator-memory",
            {
                "type": "stdio",
                "command": "operator-memory-server",
                "args": ["--stdio"],
                "env": {"OPERATOR_TOKEN": "secret-at-install-time"},
                "timeout": 30,
            },
        )
    ]
    assert sandbox.connected == ["operator-memory"]
    # GET and POST each fetched the configured operator source; neither used
    # the built-in-only compatibility index.
    assert _RemoteClient.requested == [
        "https://catalog.example.test/openbox.json",
        "https://catalog.example.test/openbox.json",
    ]


@pytest.mark.asyncio
async def test_remote_catalog_install_never_overwrites_unproven_live_package(
    monkeypatch,
    remote_catalog,
):
    sandbox = _Sandbox(
        installed=[
            {
                "name": "operator-helper",
                "install_dir": "operator-helper",
                "source": "container",
            }
        ]
    )

    async def client_for(*, user_id: str):
        return sandbox

    from fastapi import HTTPException
    from sandbox.manager import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "get_client_any", client_for)
    with pytest.raises(HTTPException) as conflict:
        await metadata.install_from_catalog(
            metadata.InstallCatalogBody(id="operator-helper", kind="skill"),
            current_user={"user_id": "buyer"},
        )
    assert conflict.value.status_code == 409
    assert sandbox.installs == []


@pytest.mark.asyncio
async def test_invalid_or_reserved_remote_entries_never_enter_listing_or_index(
    monkeypatch,
):
    _RemoteClient.requested = []
    _RemoteClient.payload = {
        "skills": [
            {
                "id": "community:forged",
                "kind": "skill",
                "name": "forged",
                "install": {"content": "# forged"},
            },
            {
                "id": "unsafe-clone",
                "kind": "skill",
                "name": "unsafe-clone",
                "install": {"url": "file:///etc"},
            },
            {
                "id": "missing-dependency",
                "kind": "skill",
                "name": "missing-dependency",
                "requires_mcp": ["not-in-this-catalog"],
                "install": {"content": "# missing dependency"},
            },
            # An invalid override must not delete/corrupt the valid built-in.
            {
                "id": "web-research",
                "kind": "skill",
                "name": "web-research",
                "install": {"url": "ext::sh -c pwn"},
            },
        ],
        "mcp": [
            {
                "id": "wrong-kind",
                "kind": "skill",
                "name": "wrong-kind",
                "config": {"type": "stdio", "command": "ignored"},
            }
        ],
    }
    monkeypatch.setenv("OPENBOX_CATALOG_URL", "https://catalog.example.test/invalid.json")
    monkeypatch.setattr("httpx.AsyncClient", _RemoteClient)

    catalog = await load_catalog()
    ids = {entry["id"] for entry in catalog["skills"]}
    assert "community:forged" not in ids
    assert "unsafe-clone" not in ids
    assert "missing-dependency" not in ids
    assert "web-research" in ids
    assert next(entry for entry in catalog["skills"] if entry["id"] == "web-research")[
        "install"
    ].get("content")
    assert all(entry["id"] != "wrong-kind" for entry in catalog["mcp"])
