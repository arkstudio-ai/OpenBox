"""Snapshots preserve real git contents while using one warm remote call."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from snapshot import snapshot


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
