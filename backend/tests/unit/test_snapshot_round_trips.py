"""Snapshots preserve real git contents while using one warm remote call."""
import asyncio
import shlex
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


async def test_same_project_path_in_two_sandboxes_initializes_both():
    store = snapshot.Store("/workspace/.openbox/snapshots/default", "/workspace/default")
    first = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(exit_code=0)))
    second = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(exit_code=0)))
    assert await snapshot._ensure_store(first, store)
    assert await snapshot._ensure_store(second, store)
    assert await snapshot._ensure_store(first, store)
    assert first.execute.await_count == second.execute.await_count == 1


async def test_remote_processes_cannot_interleave_staging_and_tree_capture(tmp_path):
    """Two API workers have no shared asyncio lock; their sandbox still does."""
    workdir = tmp_path / "project with spaces"
    workdir.mkdir()
    store = snapshot.Store(str(tmp_path / "snapshot store"), str(workdir))

    async def start(command):
        return await asyncio.create_subprocess_shell(command, cwd=workdir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def finish(process):
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, stderr.decode()
        return stdout.decode().strip()

    async def marker(path):
        async with asyncio.timeout(5):
            while not path.exists():
                await asyncio.sleep(0.01)

    async def execute(command, **kwargs):
        return SimpleNamespace(exit_code=0, stdout=await finish(await start(command)))

    assert await snapshot._ensure_store(SimpleNamespace(execute=execute), store)
    note = workdir / "note.txt"
    note.write_text("first version")
    staged = tmp_path / "first-staged"
    release = tmp_path / "release-first"
    trying = tmp_path / "second-trying"
    barrier = (
        "import pathlib,time; "
        f"pathlib.Path({str(staged)!r}).touch(); "
        "deadline=time.monotonic()+5\n"
        f"while not pathlib.Path({str(release)!r}).exists():\n"
        " if time.monotonic()>deadline: raise TimeoutError('first writer not released')\n"
        " time.sleep(0.01)"
    )
    first = await start(store.serialized(
        f"{store.git('add -A')} && python3 -c {shlex.quote(barrier)} && {store.git('write-tree')}"))
    second = None
    try:
        await marker(staged)
        note.write_text("second version")
        second = await start(f"touch {shlex.quote(str(trying))} && " + store.serialized(
            f"{store.git('add -A')} && {store.git('write-tree')}"))
        await marker(trying)
        # A real nonblocking OS lock, rather than a timing-only assertion,
        # confirms that the first process still owns the entire transaction.
        import fcntl
        import pytest
        with open(store.gitdir + ".lock", "a") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        release.touch()
    first_hash = await finish(first)
    assert second is not None
    second_hash = await finish(second)
    assert first_hash != second_hash
    assert await finish(await start(store.git(f"show {first_hash}:note.txt"))) == "first version"
    assert await finish(await start(store.git(f"show {second_hash}:note.txt"))) == "second version"
