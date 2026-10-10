"""Real child lifetime with a Python-3.10-shaped asyncio API.

Only the identity launcher is substituted; this verifies pipe/timeout/cancel
cleanup, not guest UID isolation (which is verified on the existing ECD).
"""
import asyncio
import sys

import pytest

from tests.unit.test_action_server_file_worker import file_worker
import execution_identity


@pytest.fixture
def child_world(monkeypatch):
    monkeypatch.delattr(asyncio, "timeout", raising=False)
    monkeypatch.setattr(execution_identity, "configured_user", lambda: "fixture-worker")
    state = {"children": [], "started": asyncio.Event()}
    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        child = await original(*args, **kwargs)
        state["children"].append(child)
        state["started"].set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    def program(source):
        monkeypatch.setattr(execution_identity, "prepare_child",
            lambda _argv, _env: ([sys.executable, "-I", "-c", source], {}))

    state["program"] = program
    return state


async def test_python310_internal_worker_operation_returns_its_real_response(child_world):
    child_world["program"]("import sys;sys.stdin.buffer.read();sys.stdout.write('{\"status\":200,\"headers\":[]}\\n{\"initialized\":true}')")
    assert await file_worker.json_operation("skill_initialize", {}) == {"initialized": True}
    assert child_world["children"][0].returncode == 0


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_python310_worker_timeout_or_cancel_terminates_original_child(child_world, monkeypatch, stop):
    child_world["program"]("import time;time.sleep(60)")
    monkeypatch.setattr(file_worker, "TIMEOUT", 0.03 if stop == "timeout" else 30)
    task = asyncio.create_task(file_worker.json_operation("skill_initialize", {}))
    await asyncio.wait_for(child_world["started"].wait(), timeout=2)
    if stop == "cancel":
        task.cancel()
    with pytest.raises(asyncio.TimeoutError if stop == "timeout" else asyncio.CancelledError):
        await task
    child = child_world["children"][0]
    assert child.returncode is not None and child.returncode != 0
    assert child.stdin.is_closing()
