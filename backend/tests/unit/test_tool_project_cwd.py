"""File tools execute inside the Session project while keeping public paths stable."""

from types import SimpleNamespace

import pytest

from project.workspace import namespaced_project_directory
from tool.apply_patch import ApplyPatchArgs, execute as execute_patch
from tool.glob_tool import GlobArgs, execute as execute_glob
from tool.grep import GrepArgs, execute as execute_grep
from tool.read import ReadArgs, execute as execute_read
from tool.tool import ToolContext
from tool.write import WriteArgs, execute as execute_write


USER_ID = "alice@example.com"
PROJECT_ID = "project-1"
WORKDIR = namespaced_project_directory(USER_ID, PROJECT_ID, "中文项目")


class Sandbox:
    def __init__(self):
        self.reads = []
        self.writes = []
        self.deletes = []
        self.searches = []
        self.commands = []

    async def read_file(self, path, offset=0, limit=2000):
        self.reads.append((path, offset, limit))
        return "     1\t你好"

    async def write_file(self, path, content):
        self.writes.append((path, content))

    async def delete_file(self, path):
        self.deletes.append(path)

    async def execute(self, command, **kwargs):
        self.commands.append((command, kwargs))
        return SimpleNamespace(exit_code=1, stdout="", stderr="")

    async def grep(self, **kwargs):
        self.searches.append(("grep", kwargs))
        return ""

    async def glob(self, **kwargs):
        self.searches.append(("glob", kwargs))
        return []


def _context(sandbox=None):
    return ToolContext(
        session_id="session-1",
        user_id=USER_ID,
        project_id=PROJECT_ID,
        workdir=WORKDIR,
        sandbox=sandbox or Sandbox(),
    )


def test_project_path_resolution_supports_relative_and_legacy_workspace_paths():
    ctx = _context()
    expected = f"{WORKDIR}/中文目录/你好.txt"

    assert ctx.resolve_file_path("中文目录/你好.txt") == expected
    assert ctx.resolve_file_path("/workspace/中文目录/你好.txt") == expected
    assert ctx.resolve_file_path(expected) == expected
    assert ctx.resolve_file_path("/workspace") == WORKDIR

    with pytest.raises(ValueError, match="escapes the current project"):
        ctx.resolve_file_path("../escape.txt")
    with pytest.raises(ValueError, match="escapes the current project"):
        ctx.resolve_file_path("/workspace/openbox/users/u-other/projects/p-other/x")


@pytest.mark.asyncio
async def test_read_and_write_send_canonical_paths_but_keep_relative_titles():
    sandbox = Sandbox()
    ctx = _context(sandbox)
    relative = "中文目录/你好.txt"
    expected = f"{WORKDIR}/{relative}"

    write_result = await execute_write(
        WriteArgs(file_path=relative, content="你好，OpenBox"),
        ctx,
    )
    read_result = await execute_read(ReadArgs(file_path=relative), ctx)

    assert sandbox.writes[0] == (expected, "你好，OpenBox")
    assert sandbox.reads[0] == (expected, 0, 2000)
    assert write_result.title == f"Wrote {relative}"
    assert read_result.title == relative


@pytest.mark.asyncio
async def test_patch_and_search_rebase_legacy_workspace_to_the_project():
    sandbox = Sandbox()
    ctx = _context(sandbox)

    patch_result = await execute_patch(
        ApplyPatchArgs(
            patch=(
                "*** Begin Patch\n"
                "*** Add File: /workspace/中文目录/你好.txt\n"
                "+第一行\n"
                "+第二行\n"
                "*** End Patch"
            )
        ),
        ctx,
    )
    await execute_grep(GrepArgs(pattern="你好", path="/workspace"), ctx)
    await execute_glob(GlobArgs(pattern="**/*.txt", path="."), ctx)

    assert sandbox.writes == [
        (f"{WORKDIR}/中文目录/你好.txt", "第一行\n第二行")
    ]
    assert patch_result.output == "Added /workspace/中文目录/你好.txt"
    assert sandbox.searches == [
        (
            "grep",
            {
                "pattern": "你好",
                "path": WORKDIR,
                "file_type": None,
                "include_sensitive": False,
            },
        ),
        (
            "glob",
            {
                "pattern": "**/*.txt",
                "path": WORKDIR,
                "include_sensitive": False,
            },
        ),
    ]
