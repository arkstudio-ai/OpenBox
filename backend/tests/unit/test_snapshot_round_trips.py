"""Snapshots preserve real git contents while using one warm remote call."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from snapshot import snapshot


async def test_real_git_paths_match_in_stats_and_review_hunks(tmp_path, monkeypatch):
    workdir = tmp_path / "project"
    workdir.mkdir()
    store = snapshot.Store(str(tmp_path / "snapshots"), str(workdir))
    monkeypatch.setattr(snapshot, "_store", AsyncMock(return_value=store))
    async def execute(command, *, workdir, **kwargs):
        process = await asyncio.create_subprocess_shell(command, cwd=workdir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        return SimpleNamespace(exit_code=process.returncode, stdout=stdout.decode(), stderr=stderr.decode())
    sandbox = SimpleNamespace(execute=execute)
    names = ['读书会签到表.csv', '有 空格.txt', '双"引号.txt', '反\\斜线.txt', '有\t制表符.txt',
             '有\n换行.txt', 'emoji-🍎.txt']
    (workdir / '删除中文.txt').write_text('旧文件\n')
    (workdir / '修改中文.txt').write_text('保留\n')
    first = await snapshot.track('paths', sandbox)
    (workdir / '删除中文.txt').unlink()
    (workdir / '修改中文.txt').write_text('保留\n新增\n')
    for name in names:
        (workdir / name).write_text('姓名,到场状态\n林晓,已到\n')
    second = await snapshot.track('paths', sandbox)
    stats = await snapshot.diff(first, second, sandbox, 'paths')
    hunks = await snapshot.diff_full(first, second, sandbox, 'paths')
    expected = set(names + ['删除中文.txt', '修改中文.txt'])
    assert {row.path for row in stats} == expected
    assert {row['path'] for row in hunks} == expected
    assert {row.path: row.status for row in stats} == {row['path']: row['status'] for row in hunks}
    assert all(row['hunks'] for row in hunks)


def test_old_patch_projection_decodes_git_once_without_changing_stored_data():
    from db.models.part import public_part_data
    from models.message import PatchPart, PatchFile
    path = '读书会签到表.csv'
    encoded = '"' + ''.join(f'\\{byte:03o}' for byte in path.encode()) + '"'
    old = {'type': 'patch', 'files': [{'path': encoded}], 'canonical_tool_id': 'private'}
    before = json.dumps(old)
    projected = public_part_data(old)
    assert projected['files'][0]['path'] == path
    assert json.dumps(old) == before and 'canonical_tool_id' not in projected
    assert public_part_data(projected) == projected
    literal = PatchPart(files=[PatchFile(path=encoded)]).model_dump()
    assert public_part_data(literal)['files'][0]['path'] == encoded


async def test_combined_snapshot_preserves_contents_and_serializes_writers(tmp_path, monkeypatch):
    workdir = tmp_path / "project"
    workdir.mkdir()
    (workdir / "note.txt").write_text("first version")
    store = snapshot.Store(str(tmp_path / "snapshots"), str(workdir))
    monkeypatch.setattr(snapshot, "_store", AsyncMock(return_value=store))
    calls = []
    active = maximum = 0

    async def execute(command, *, workdir, **kwargs):
        nonlocal active, maximum
        calls.append(command)
        active += 1
        maximum = max(maximum, active)
        try:
            process = await asyncio.create_subprocess_shell(
                command, cwd=workdir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await process.communicate()
            return SimpleNamespace(exit_code=process.returncode, stdout=stdout.decode(), stderr=stderr.decode())
        finally:
            active -= 1

    sandbox = SimpleNamespace(execute=execute)
    first = await snapshot.track("session-a", sandbox)
    assert first and len(calls) == 2  # initialize, then stage + write-tree
    (workdir / "note.txt").write_text("second version")
    changed, unchanged = await asyncio.gather(
        snapshot.track("session-a", sandbox), snapshot.track("session-b", sandbox))
    assert changed == unchanged and changed != first
    assert len(calls) == 4 and maximum == 1
    saved = await execute(store.git(f"show {changed}:note.txt"), workdir=str(workdir))
    assert saved.exit_code == 0 and saved.stdout == "second version"


async def test_failed_staging_does_not_return_a_tree(tmp_path, monkeypatch):
    store = snapshot.Store(str(tmp_path / "absent"), str(tmp_path))
    monkeypatch.setattr(snapshot, "_store", AsyncMock(return_value=store))
    monkeypatch.setattr(snapshot, "_ensure_store", AsyncMock(return_value=True))
    sandbox = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        exit_code=128, stdout="", stderr="staging failed")))
    assert await snapshot.track("session-a", sandbox) is None
