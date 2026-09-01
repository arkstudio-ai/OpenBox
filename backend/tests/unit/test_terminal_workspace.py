from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from api import terminal
from project.workspace import user_scope_for_identity


@pytest.mark.asyncio
async def test_terminal_workspace_resolves_owned_session_server_side(monkeypatch):
    async def get_session(session_id: str, *, user_id: str):
        assert (session_id, user_id) == ("session-你好", "alice")
        return SimpleNamespace(project_id="project-1")

    async def get_project(project_id: str, user_id: str):
        assert (project_id, user_id) == ("project-1", "alice")
        return SimpleNamespace(id=project_id, name="中文项目")

    async def workdir(user_id: str, project_id: str):
        return f"/workspace/openbox/users/{user_scope_for_identity(user_id)}/projects/{project_id}/资料"

    monkeypatch.setattr(terminal.session_mod, "get_session", get_session)
    monkeypatch.setattr(terminal, "get_project", get_project)
    monkeypatch.setattr(terminal, "workdir_for_identity", workdir)

    directory, scope, label = await terminal._terminal_workspace(
        "alice",
        session_id="session-你好",
    )

    assert directory.endswith("/project-1/资料")
    assert scope == user_scope_for_identity("alice")
    assert label == "中文项目"


@pytest.mark.asyncio
async def test_terminal_workspace_rejects_session_project_mismatch(monkeypatch):
    async def get_session(*_args, **_kwargs):
        return SimpleNamespace(project_id="project-owned")

    monkeypatch.setattr(terminal.session_mod, "get_session", get_session)

    with pytest.raises(PermissionError):
        await terminal._terminal_workspace(
            "alice",
            session_id="session-1",
            project_id="project-other",
        )


@pytest.mark.asyncio
async def test_terminal_workspace_without_a_session_uses_the_owned_default(monkeypatch):
    async def ensure_default(user_id: str):
        assert user_id == "alice"
        return SimpleNamespace(id="project-default", name="Default")

    async def workdir(user_id: str, project_id: str):
        assert (user_id, project_id) == ("alice", "project-default")
        return "/workspace/openbox/users/u-default/projects/p-default"

    monkeypatch.setattr(terminal, "ensure_default_project", ensure_default)
    monkeypatch.setattr(terminal, "workdir_for_identity", workdir)

    directory, scope, label = await terminal._terminal_workspace("alice")

    assert directory.endswith("/p-default")
    assert scope == user_scope_for_identity("alice")
    assert label == "Default"


def test_container_terminal_url_encodes_credentials_scope_and_unicode_path():
    info = SimpleNamespace(host="127.0.0.1", port=18000, api_key="secret +/=?")
    expected_scope = user_scope_for_identity("alice")

    url = terminal._container_terminal_url(
        info,
        workdir="/workspace/项目/资料",
        user_scope=expected_scope,
        prompt_label="中文项目",
    )
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.netloc == "127.0.0.1:18000"
    assert query == {
        "api_key": ["secret +/=?"],
        "workdir": ["/workspace/项目/资料"],
        "user_scope": [expected_scope],
        "prompt_label": ["中文项目"],
    }


@pytest.mark.asyncio
async def test_terminal_requires_project_cwd_capability(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"capabilities": ["terminal_project_cwd_v1"]}

    async def forward(container_id, method, path, *, user_id, timeout):
        assert (container_id, method, path, user_id, timeout) == (
            "desktop",
            "GET",
            "/alive",
            "alice",
            5.0,
        )
        return Response()

    monkeypatch.setattr(terminal.provider, "forward_to_container", forward)
    await terminal._require_project_terminal_capability(
        "desktop", user_id="alice"
    )


@pytest.mark.asyncio
async def test_terminal_rejects_legacy_action_server(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"capabilities": ["mcp_supervisor_v1"]}

    async def forward(*_args, **_kwargs):
        return Response()

    monkeypatch.setattr(terminal.provider, "forward_to_container", forward)
    with pytest.raises(RuntimeError, match="component update"):
        await terminal._require_project_terminal_capability(
            "desktop", user_id="alice"
        )
