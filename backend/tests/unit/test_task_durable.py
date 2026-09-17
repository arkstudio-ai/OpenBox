"""Durable ownership and abort ordering for Task child agents."""

import asyncio
import inspect

import pytest

from tool.tool import ToolContext


class FakeLease:
    generation = 3
    run_id = "run-3"

    def __init__(self):
        self.releases: list[str | None] = []

    async def release(self, *, session_status=None):
        self.releases.append(session_status)
        return True


def test_dead_announce_child_and_private_sandbox_map_mutation_are_removed():
    import tool.task as task_mod

    source = inspect.getsource(task_mod)
    assert not hasattr(task_mod, "_announce_child")
    assert "sandbox_manager._session_project" not in source
    assert "sandbox_manager._project_map" not in source


@pytest.mark.asyncio
async def test_child_loop_receives_reserved_lease_and_fallback_releases(monkeypatch):
    import agent.loop as loop_mod
    import tool.task as task_mod

    lease = FakeLease()
    received = {}

    async def failed_before_loop_finally(session_id, user_id, *, lease):
        received.update(session_id=session_id, user_id=user_id, lease=lease)
        raise RuntimeError("startup failed")

    monkeypatch.setattr(loop_mod, "run_loop", failed_before_loop_finally)

    with pytest.raises(RuntimeError, match="startup failed"):
        await task_mod._run_child(
            ToolContext(session_id="parent", user_id="user-1"),
            "child-1",
            lease,
        )

    assert received == {
        "session_id": "child-1",
        "user_id": "user-1",
        "lease": lease,
    }
    assert lease.releases == ["error"]


@pytest.mark.asyncio
async def test_foreground_losing_activation_claim_waits_for_exact_outbox(monkeypatch):
    from types import SimpleNamespace

    import agent.driver as driver_mod
    import agent.subagent_runtime as runtime
    import tool.task as task_mod

    activation = SimpleNamespace(id="activation-1", descriptor_id="subagent-1")

    async def lost_claim(*_args, **_kwargs):
        return None

    async def ready_outbox(*_args, **_kwargs):
        return {
            "title": "owned elsewhere",
            "output": "durable result",
            "metadata": {"task_handoff_id": "activation-1"},
        }

    async def forbidden_reserve(*_args, **_kwargs):
        raise AssertionError("foreground must not reserve after losing claim")

    monkeypatch.setattr(runtime, "claim_activation", lost_claim)
    monkeypatch.setattr(runtime, "wait_for_outbox", ready_outbox)
    monkeypatch.setattr(driver_mod, "reserve_run", forbidden_reserve)
    result = await task_mod._dispatch_activation(
        ToolContext(session_id="parent", user_id="user-1"),
        activation,
        project_id="project-1",
    )
    assert result.output == "durable result"


@pytest.mark.asyncio
async def test_parent_abort_is_persisted_before_local_child_cancellation(monkeypatch):
    import agent.driver as driver_mod
    import agent.loop as loop_mod
    import tool.task as task_mod

    lease = FakeLease()
    parent_abort = asyncio.Event()
    child_started = asyncio.Event()
    order: list[str] = []

    async def wedged_loop(_session_id, user_id, *, lease):
        assert user_id == "user-2"
        child_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            order.append("cancelled")
            raise

    async def request_abort(
        session_id,
        user_id,
        *,
        expected_run_id=None,
        expected_generation=None,
    ):
        order.append(
            f"abort:{session_id}:{user_id}:{expected_run_id}:"
            f"{expected_generation}"
        )
        return True

    async def wait_for_idle(_session_id, *, timeout):
        order.append(f"idle-wait:{timeout}")
        return False

    async def immediate_timeout(awaitable, timeout):
        order.append(f"grace:{timeout}")
        # The shielded child must stay alive so _run_child performs its own
        # explicitly ordered cancellation after the durable stop request.
        raise asyncio.TimeoutError

    monkeypatch.setattr(loop_mod, "run_loop", wedged_loop)
    monkeypatch.setattr(driver_mod, "request_abort", request_abort)
    monkeypatch.setattr(driver_mod, "wait_for_idle", wait_for_idle)
    monkeypatch.setattr(asyncio, "wait_for", immediate_timeout)

    running = asyncio.create_task(
        task_mod._run_child(
            ToolContext(
                session_id="parent",
                user_id="user-2",
                abort=parent_abort,
            ),
            "child-2",
            lease,
        )
    )
    await child_started.wait()
    parent_abort.set()
    await running

    assert order == [
        "abort:child-2:user-2:run-3:3",
        "grace:10",
        "idle-wait:1.0",
        "cancelled",
    ]
    assert lease.releases == ["error"]
