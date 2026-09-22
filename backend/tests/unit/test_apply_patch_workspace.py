from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.hooks import ToolHooks
from tool.tool import ToolContext
from tool.workspace.apply_patch import ApplyPatchArgs, execute, parse_patch


async def test_patch_permission_and_execution_targets_agree_for_member_workdir(monkeypatch):
    patch = "*** Begin Patch\n*** Add File: result.txt\n+ok\n*** End Patch"
    sandbox = SimpleNamespace(write_file=AsyncMock())
    ctx = ToolContext(session_id="member", workdir="/workspace/projects/team-project", sandbox=sandbox)
    monkeypatch.setattr("trajectory.files.record_file_change", AsyncMock())
    targets = ToolHooks(session_id="member")._extract_patterns("apply_patch", {"patch": patch})
    assert targets == ["result.txt"]
    result = await execute(ApplyPatchArgs(patch=patch), ctx)
    sandbox.write_file.assert_awaited_once_with(ctx.resolve_file_path(targets[0]), "ok")
    assert "/workspace/projects/team-project/result.txt" in result.output
    assert result.metadata["error"] is False


def test_patch_extracts_every_operation_before_permission_checks():
    patch = ("*** Begin Patch\n*** Add File: first.txt\n+ok\n"
        "*** Update File: second.txt\n-before\n+after\n*** Delete File: third.txt\n*** End Patch")
    assert [(op["type"], op["path"]) for op in parse_patch(patch)] == [
        ("add", "first.txt"), ("update", "second.txt"), ("delete", "third.txt")]
    assert ToolHooks(session_id="test")._extract_patterns("apply_patch", {"patch": patch}) == [
        "first.txt", "second.txt", "third.txt"]


@pytest.mark.parametrize("patch", [
    "", "*** Begin Patch\n*** End Patch", "*** Begin Patch\n*** Add File: a\n+x",
    "*** Begin Patch\n*** Add File: \n+x\n*** End Patch",
    "*** Begin Patch\n*** Update File: a\n*** Move to: b\n*** End Patch",
])
async def test_invalid_patch_fails_before_file_effects(patch):
    sandbox = SimpleNamespace(write_file=AsyncMock(), execute=AsyncMock(), read_file=AsyncMock())
    with pytest.raises(ValueError):
        await execute(ApplyPatchArgs(patch=patch), ToolContext(sandbox=sandbox))
    sandbox.write_file.assert_not_awaited()
    sandbox.execute.assert_not_awaited()
    sandbox.read_file.assert_not_awaited()
