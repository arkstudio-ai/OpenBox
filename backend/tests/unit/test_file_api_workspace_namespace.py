"""The backend file proxy cannot be pointed at another tenant namespace."""

import pytest
from fastapi import HTTPException

from api import files as files_api
from api.files import _project_relative_hits, _tenant_path
from project.workspace import user_directory


def test_workspace_alias_resolves_to_authenticated_tenant_root():
    assert _tenant_path("alice", "/workspace") == user_directory("alice")


def test_own_nested_path_is_accepted():
    root = user_directory("alice")
    path = f"{root}/projects/p-deadbeef-demo/src/main.py"
    assert _tenant_path("alice", path) == path


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/workspace/openbox/users/u-someone-else/projects/demo",
        "/workspace/openbox/users/u-fake/../u-other/projects/demo",
    ],
)
def test_other_or_traversing_paths_are_rejected(path):
    with pytest.raises(HTTPException) as error:
        _tenant_path("alice", path)
    assert error.value.status_code == 403


def test_search_results_are_project_relative_and_preserve_unicode():
    root = f"{user_directory('alice')}/projects/p-demo"

    assert _project_relative_hits(
        root,
        [
            f"{root}/资料/设计稿-你好😀.md",
            f"{root}/src/main.py",
            f"{root}/node_modules/pkg/index.js",
            f"{user_directory('alice')}/projects/p-other/secret.txt",
            f"{root}/../p-other/escape.txt",
        ],
        "你好",
    ) == ["资料/设计稿-你好😀.md"]


def test_relative_action_server_results_stay_relative_to_the_project():
    root = f"{user_directory('alice')}/projects/p-demo"

    assert _project_relative_hits(root, ["src/入口.ts", "../secret"], "") == [
        "src/入口.ts"
    ]


@pytest.mark.asyncio
async def test_file_search_forwards_the_owned_project_root_and_returns_relative_hits(
    monkeypatch,
):
    root = f"{user_directory('alice')}/projects/p-demo"

    async def scope(user_id, *, session_id="", project_id=""):
        assert (user_id, session_id, project_id) == ("alice", "session-一", "project-一")
        return root

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {"files": [f"{root}/资料/你好😀.txt"]}

    async def forward(container_id, method, endpoint, *, user_id, json):
        assert (container_id, method, endpoint, user_id) == (
            "desktop",
            "POST",
            "/glob",
            "alice",
        )
        assert json == {"pattern": "**/*", "path": root}
        return Response()

    monkeypatch.setattr(files_api, "_file_scope_root", scope)
    monkeypatch.setattr(files_api.provider, "forward_to_container", forward)

    result = await files_api.search_files(
        "desktop",
        q="你好",
        session_id="session-一",
        project_id="project-一",
        current_user={"user_id": "alice"},
    )

    assert result == {"files": ["资料/你好😀.txt"], "total": 1}


@pytest.mark.asyncio
async def test_project_scoped_search_rejects_an_explicit_sibling_path(monkeypatch):
    root = f"{user_directory('alice')}/projects/p-demo"

    async def scope(*_args, **_kwargs):
        return root

    monkeypatch.setattr(files_api, "_file_scope_root", scope)
    with pytest.raises(HTTPException) as error:
        await files_api.search_files(
            "desktop",
            path=f"{user_directory('alice')}/projects/p-other",
            session_id="session-一",
            current_user={"user_id": "alice"},
        )
    assert error.value.status_code == 403
