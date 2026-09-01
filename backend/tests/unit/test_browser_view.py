from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from sandbox.browser_view import (
    BrowserViewController,
    BrowserViewProtocolError,
    normalize_navigation_url,
)
from sandbox.client import ExecuteResult


USER_SCOPE = "u-" + "a" * 20


class FakeClient:
    def __init__(self):
        self.commands: list[str] = []
        self.downloads: list[tuple[str, int]] = []
        self.deleted: list[str] = []
        self.leases: list[dict] = []

    async def execute(self, command, timeout=120, workdir="/workspace"):
        self.commands.append(command)
        if "/json/list" in command:
            return ExecuteResult(
                0,
                '[{"type":"page","url":"https://example.com/中文"}]',
                "",
            )
        return ExecuteResult(0, "", "")

    async def download_file_bytes(self, path, *, max_bytes):
        self.downloads.append((path, max_bytes))
        return b"\x89PNG\r\n\x1a\nframe"

    @asynccontextmanager
    async def desktop_lease(self, **kwargs):
        self.leases.append(kwargs)
        yield {"token": "lease"}

    async def delete_file(self, path):
        self.deleted.append(path)


def test_navigation_url_is_http_only_bounded_and_credential_free():
    assert normalize_navigation_url(" example.com/中文#local ") == (
        "https://example.com/%E4%B8%AD%E6%96%87"
    )
    assert normalize_navigation_url("HTTP://example.com/a?q=1") == "http://example.com/a?q=1"

    for invalid in (
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://user:pass@example.com/",
        "https://",
        "https://example.com/\nnext",
        "x" * 2049,
    ):
        with pytest.raises(BrowserViewProtocolError):
            normalize_navigation_url(invalid)


@pytest.mark.asyncio
async def test_start_prepares_scoped_frames_and_reports_current_url(monkeypatch):
    async def ready(_client, _key):
        return None

    monkeypatch.setattr("sandbox.browser_view.ensure_desktop_tools", ready)
    monkeypatch.setattr("sandbox.browser_view.ensure_chrome", ready)
    client = FakeClient()
    controller = BrowserViewController(
        client,
        container_key="desktop",
        user_scope=USER_SCOPE,
    )

    assert await controller.start() == "https://example.com/中文"
    assert controller.frame_dir == f"/workspace/openbox/users/{USER_SCOPE}/.browser-view"
    assert any(command.startswith("install -d -m 0700 -- ") for command in client.commands)
    assert all("sudo" not in command for command in client.commands)


@pytest.mark.asyncio
async def test_capture_uses_unique_tenant_path_and_accepts_only_png(monkeypatch):
    client = FakeClient()
    controller = BrowserViewController(
        client,
        container_key="desktop",
        user_scope=USER_SCOPE,
    )
    geometry = {"native": [1920, 1080], "scaled": [1280, 720], "bytes": 13}

    async def shot(_client, dest):
        assert dest == controller.frame_path
        return geometry

    monkeypatch.setattr("sandbox.browser_view.take_screenshot", shot)
    returned_geometry, frame = await controller.capture()

    assert returned_geometry == geometry
    assert frame.startswith(b"\x89PNG")
    assert client.downloads == [(controller.frame_path, 8 * 1024 * 1024)]


@pytest.mark.asyncio
async def test_commands_are_bounded_and_take_a_short_desktop_lease():
    client = FakeClient()
    controller = BrowserViewController(
        client,
        container_key="desktop",
        user_scope=USER_SCOPE,
    )
    controller.geometry = {"native": [3840, 2160], "scaled": [1280, 720]}

    response = await controller.handle({"type": "navigate", "url": "example.com/?q=a b"})
    await controller.handle({"type": "click", "x": 640, "y": 360, "button": 0})
    await controller.handle({"type": "scroll", "dx": 0, "dy": 9000})
    await controller.handle({"type": "key", "key": "Enter"})

    assert response == {"type": "navigated", "url": "https://example.com/?q=a%20b"}
    assert len(client.leases) == 4
    assert all(lease["operation"] == "browser-view-input" for lease in client.leases)
    assert any("mousemove 1920 1080 click 1" in command for command in client.commands)
    assert any("click --repeat 20" in command for command in client.commands)

    with pytest.raises(BrowserViewProtocolError):
        await controller.handle({"type": "key", "key": "Control+Alt+Delete"})
    with pytest.raises(BrowserViewProtocolError):
        await controller.handle({"type": "navigate", "url": "javascript:alert(1)"})


@pytest.mark.asyncio
async def test_close_removes_only_this_connections_transient_frame():
    client = FakeClient()
    controller = BrowserViewController(
        client,
        container_key="desktop",
        user_scope=USER_SCOPE,
    )

    await controller.close()

    assert client.deleted == [controller.frame_path]
