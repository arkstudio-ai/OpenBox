"""Terminal Session state follows the exact durable agent generation."""
import asyncio
import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

from agent import loop
from agent import processor
from agent.driver import LeaseLostError
from models.message import SessionStatus


class FakeLease:
    def __init__(
        self,
        *,
        release_result: bool = True,
        preserve_result: bool = True,
        cancel_on_phase: bool = False,
        preserve_error: Exception | None = None,
    ):
        self.session_id = "settle-session"
        self.user_id = "settle-user"
        self.run_id = "settle-run"
        self.generation = 7
        self.abort = asyncio.Event()
        self.release_result = release_result
        self.preserve_result = preserve_result
        self.cancel_on_phase = cancel_on_phase
        self.preserve_error = preserve_error
        self.closed = False
        self.events: list[tuple[str, str | None]] = []

    async def release(self, *, session_status=None):
        self.events.append(("release", session_status))
        if self.closed:
            return False
        self.closed = True
        return self.release_result

    async def preserve_for_recovery(self, *, session_status="error"):
        self.events.append(("preserve", session_status))
        if self.preserve_error is not None:
            raise self.preserve_error
        if self.closed:
            return False
        self.closed = True
        return self.preserve_result

    async def set_phase(self, _phase):
        if self.cancel_on_phase:
            raise asyncio.CancelledError


def test_run_owned_transcript_calls_keep_transaction_fence_keyword():
    watched = {
        "create_assistant_message",
        "create_compaction",
        "process_compaction",
        "prune_tool_outputs",
        "save_part",
        "set_session_status",
        "update_message_info",
        "update_part_data",
        "update_session",
        "update_session_tokens",
    }
    missing: list[str] = []
    for function in (loop.run_loop, processor.process_step):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            target = call.func
            name = (
                target.id
                if isinstance(target, ast.Name)
                else target.attr
                if isinstance(target, ast.Attribute)
                else ""
            )
            if name in watched and not any(
                keyword.arg == "run_fence" for keyword in call.keywords
            ):
                missing.append(f"{function.__name__}:{call.lineno}:{name}")
    assert missing == []


@pytest.mark.asyncio
async def test_success_status_is_published_only_after_atomic_release(monkeypatch):
    lease = FakeLease()
    events: list[tuple[str, str | None]] = []

    async def release(*, session_status=None):
        events.append(("release", session_status))
        return True

    lease.release = release
    monkeypatch.setattr(
        loop.bus,
        "publish",
        lambda _event, payload: events.append(("publish", payload["status"])),
    )

    await loop._settle_run_status(
        lease,
        session_id=lease.session_id,
        user_id=lease.user_id,
        status=SessionStatus.IDLE,
    )

    assert events == [("release", "idle"), ("publish", "idle")]


@pytest.mark.asyncio
async def test_stale_generation_cannot_publish_terminal_status(monkeypatch):
    lease = FakeLease(release_result=False)
    published = []
    monkeypatch.setattr(loop.bus, "publish", lambda *args: published.append(args))

    with pytest.raises(LeaseLostError):
        await loop._settle_run_status(
            lease,
            session_id=lease.session_id,
            user_id=lease.user_id,
            status=SessionStatus.IDLE,
        )

    assert published == []


@pytest.mark.asyncio
async def test_first_session_read_failure_preserves_passed_lease(monkeypatch):
    lease = FakeLease()
    published = []

    async def fail_session_read(*_args, **_kwargs):
        raise RuntimeError("session read failpoint")

    monkeypatch.setattr(loop, "get_session", fail_session_read)
    monkeypatch.setattr(loop.bus, "publish", lambda event, data: published.append((event, data)))

    assert await loop.run_loop(
        lease.session_id,
        user_id=lease.user_id,
        lease=lease,
    ) is None

    assert lease.events[0] == ("preserve", "error")
    assert lease.events[-1] == ("release", "error")
    assert any(data.get("status") == "error" for _, data in published)


@pytest.mark.asyncio
async def test_cancellation_preserves_marker_before_propagating(monkeypatch):
    lease = FakeLease(cancel_on_phase=True)
    published = []

    async def get_session(*_args, **_kwargs):
        return SimpleNamespace(project_id="project")

    monkeypatch.setattr(loop, "get_session", get_session)
    monkeypatch.setattr(loop.bus, "publish", lambda event, data: published.append((event, data)))

    with pytest.raises(asyncio.CancelledError):
        await loop.run_loop(
            lease.session_id,
            user_id=lease.user_id,
            lease=lease,
        )

    assert lease.events[0] == ("preserve", "error")
    assert lease.events[-1] == ("release", "error")
    assert any(data.get("status") == "error" for _, data in published)


@pytest.mark.asyncio
async def test_finalizer_atomically_sets_error_when_first_preserve_fails(monkeypatch):
    lease = FakeLease(preserve_error=RuntimeError("preserve failpoint"))
    published = []

    async def fail_session_read(*_args, **_kwargs):
        raise RuntimeError("session read failpoint")

    monkeypatch.setattr(loop, "get_session", fail_session_read)
    monkeypatch.setattr(
        loop.bus,
        "publish",
        lambda event, data: published.append((event, data)),
    )

    assert await loop.run_loop(
        lease.session_id,
        user_id=lease.user_id,
        lease=lease,
    ) is None

    assert lease.events == [
        ("preserve", "error"),
        ("release", "error"),
    ]
    assert any(data.get("status") == "error" for _, data in published)
