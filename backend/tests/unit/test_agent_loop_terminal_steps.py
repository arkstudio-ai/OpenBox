"""Real-loop regressions for terminal Assistant/Step boundaries."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from agent import loop
from agent.driver import reserve_run
from agent.inbox import accept_inbox_item
from agent.processor import StepOutcome, StepResult
from agent.structured_output import TOOL_NAME as STRUCTURED_OUTPUT_TOOL
from agent.tool_exposure import ExposureSignals
from core.config import ProviderConfig, get_config
from db.base import get_db_session
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from session.agent_event_log import verify_agent_event_parity
from session.event_range import freeze_fork_event_range
from session.fork import fork_session
from session.session import create_user_message, get_messages


SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


def _loop_config():
    config = get_config().model_copy(deep=True)
    config.provider["openai"] = ProviderConfig(
        api_key="terminal-step-test-key",
        base_url="https://terminal-step.invalid/v1",
    )
    config.model = "openai/terminal-step-test"
    config.models = []
    config.tool_exposure.mode = "legacy_eager"
    return config


async def _seed_session(*, model: str, output_format: dict | None):
    suffix = uuid.uuid4().hex[:12]
    user_id = f"terminal-user-{suffix}"
    project_id = f"terminal-project-{suffix}"
    session_id = f"terminal-session-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=user_id,
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name="Terminal step integration",
            slug=project_id,
            created_at=now,
            updated_at=now,
        ))
        db.add(Session(
            id=session_id,
            user_id=user_id,
            project_id=project_id,
            title="Terminal step integration",
            agent="build",
            model=model,
            status="idle",
            token_usage={},
            tool_exposure_state={},
            created_at=now,
            updated_at=now,
        ))
    lease = await reserve_run(session_id, user_id)
    user = await create_user_message(
        session_id,
        "Return the requested shape.",
        model=model,
        output_format=output_format,
        user_id=user_id,
        run_fence=(session_id, lease.run_id, lease.generation),
        bind_trigger=True,
    )
    return user_id, project_id, session_id, user, lease


def _patch_real_loop_runtime(monkeypatch, *, config, process_step) -> None:
    """Leave Session/Event/Driver writes real while replacing external I/O."""

    async def _none(*_args, **_kwargs):
        return None

    async def _sandbox(*_args, **_kwargs):
        return SimpleNamespace()

    async def _slug(*_args, **_kwargs):
        return "terminal-project"

    async def _workdir(*_args, **_kwargs):
        return "/workspace/terminal-project"

    async def _tools(*_args, **_kwargs):
        return SimpleNamespace(tools={}, catalogue_availability="available")

    async def _signals(*_args, **_kwargs):
        return ExposureSignals()

    async def _system(*_args, **_kwargs):
        return ["deterministic test system"]

    async def _history_names(*_args, **_kwargs):
        return {}

    async def _messages(messages, *_args, **_kwargs):
        return messages

    async def _snapshot(*_args, **_kwargs):
        return "snapshot-terminal"

    async def _not_overflow(*_args, **_kwargs):
        return False

    async def _no_notices(*_args, **_kwargs):
        return []

    async def _ack_notices(*_args, **_kwargs):
        return True

    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", _sandbox)
    monkeypatch.setattr(loop, "ensure_directory", _none)
    monkeypatch.setattr(loop, "slug_for", _slug)
    monkeypatch.setattr(loop, "workdir_for_session", _workdir)
    monkeypatch.setattr(loop, "resolve_step_tools", _tools)
    monkeypatch.setattr(loop, "collect_exposure_signals", _signals)
    monkeypatch.setattr(loop, "_build_system_prompt", _system)
    monkeypatch.setattr(loop, "_resolve_history_tool_names", _history_names)
    monkeypatch.setattr(loop, "_insert_todo_pacing", _messages)
    monkeypatch.setattr(loop, "resolve_images", _messages)
    monkeypatch.setattr(loop.snapshot, "track", _snapshot)
    monkeypatch.setattr(loop, "is_overflow", _not_overflow)
    monkeypatch.setattr(loop, "prune_tool_outputs", _none)
    monkeypatch.setattr(loop, "get_model_context_limit", lambda *_args: 128_000)
    monkeypatch.setattr(loop, "process_step", process_step)
    monkeypatch.setattr(loop.bus, "publish", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("cron.injector.flush_pending_cron_results", _none)
    monkeypatch.setattr("session.abort.settle_running_todos", _none)
    monkeypatch.setattr("session.todo.pending_notices", _no_notices)
    monkeypatch.setattr("session.todo.acknowledge_notices", _ack_notices)
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_args: None)


async def _assert_balanced_steps(session_id: str, user_id: str, count: int):
    async with get_db_session() as db:
        assistants = list((await db.execute(
            select(Message)
            .where(
                Message.session_id == session_id,
                Message.user_id == user_id,
                Message.role == "assistant",
            )
            .order_by(Message.created_at, Message.id)
        )).scalars().all())
        parts = list((await db.execute(
            select(Part)
            .where(
                Part.session_id == session_id,
                Part.user_id == user_id,
                Part.type.in_(("step-start", "step-finish")),
            )
            .order_by(Part.created_at, Part.id)
        )).scalars().all())
    assert len(assistants) == count
    assert [part.type for part in parts].count("step-start") == count
    assert [part.type for part in parts].count("step-finish") == count
    for assistant in assistants:
        owned = [part.type for part in parts if part.message_id == assistant.id]
        assert owned.count("step-start") == 1
        assert owned.count("step-finish") == 1
    return assistants


@pytest.mark.asyncio
async def test_structured_terminal_with_pending_next_step_closes_and_forks(
    ensure_test_db,
    monkeypatch,
):
    config = _loop_config()
    user_id, _project_id, session_id, _user, lease = await _seed_session(
        model=config.model,
        output_format=SCHEMA,
    )
    provider_calls = 0

    async def _structured_step(**kwargs):
        nonlocal provider_calls
        provider_calls += 1
        tool = kwargs["tools"][STRUCTURED_OUTPUT_TOOL]
        result = await tool.execute(
            {"answer": f"answer-{provider_calls}"},
            kwargs["ctx"],
        )
        assert result.metadata["structured"] is True
        if provider_calls == 1:
            await accept_inbox_item(
                session_id=session_id,
                user_id=user_id,
                delivery="steer",
                prompt="Recheck and return the same schema.",
                output_format=SCHEMA,
                model=config.model,
                client_id=f"terminal-steer-{uuid.uuid4().hex[:8]}",
            )
        return StepResult(
            outcome=StepOutcome.CONTINUE,
            finish_reason="tool_calls",
            usage={
                "input": 11 * provider_calls,
                "output": 3 * provider_calls,
                "total": 14 * provider_calls,
                "cost": 0.01 * provider_calls,
            },
            duration=0.25 * provider_calls,
        )

    _patch_real_loop_runtime(
        monkeypatch,
        config=config,
        process_step=_structured_step,
    )
    result = await loop.run_loop(session_id, user_id=user_id, lease=lease)

    assert provider_calls == 2
    assert result is not None
    assert result.structured == {"answer": "answer-2"}
    messages = await get_messages(session_id, user_id=user_id)
    assert [message.role.value for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assistants = await _assert_balanced_steps(session_id, user_id, 2)
    assert [assistant.finish for assistant in assistants] == ["stop", "stop"]
    parity = await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )
    assert parity.ok is True, parity.model_dump()

    frozen = await freeze_fork_event_range(
        session_id,
        user_id=user_id,
        up_to_message_id=assistants[-1].id,
    )
    assert tuple(message.id for message in messages) == frozen.covered_message_ids
    child = await fork_session(
        session_id,
        up_to_message_id=assistants[-1].id,
        user_id=user_id,
    )
    child_parity = await verify_agent_event_parity(
        child.id,
        user_id=user_id,
        require_closed=True,
    )
    assert child_parity.ok is True, child_parity.model_dump()


@pytest.mark.asyncio
async def test_third_compaction_failure_closes_exact_real_step(
    ensure_test_db,
    monkeypatch,
):
    config = _loop_config()
    user_id, _project_id, session_id, _user, lease = await _seed_session(
        model=config.model,
        output_format=None,
    )
    provider_calls = 0

    async def _compact_step(**kwargs):
        nonlocal provider_calls
        provider_calls += 1
        # A real overflow queues a compaction continuation before returning
        # ``compact``. Keep the first two attempts runnable; the third is the
        # exact response the loop converts into its terminal orchestration
        # error, so it must not manufacture another dangling User boundary.
        if provider_calls < 3:
            await create_user_message(
                session_id,
                "Continue after failed compaction.",
                agent="build",
                model=config.model,
                synthetic=True,
                user_id=user_id,
                run_fence=kwargs["ctx"].run_fence,
            )
        return StepResult(
            outcome=StepOutcome.CONTINUE,
            finish_reason="compact",
            usage={"input": 5, "output": 1, "total": 6, "cost": 0.0},
            duration=0.1,
        )

    _patch_real_loop_runtime(
        monkeypatch,
        config=config,
        process_step=_compact_step,
    )
    assert await loop.run_loop(session_id, user_id=user_id, lease=lease) is None

    assert provider_calls == 3
    assistants = await _assert_balanced_steps(session_id, user_id, 3)
    assert [assistant.finish for assistant in assistants] == [
        "compact",
        "compact",
        "error",
    ]
    assert assistants[-1].error == {
        "code": "COMPACTION_FAILED",
        "message": (
            "Context too large and compaction failed. "
            "Please start a new session."
        ),
    }
    parity = await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )
    assert parity.ok is True, parity.model_dump()
    frozen = await freeze_fork_event_range(
        session_id,
        user_id=user_id,
        up_to_message_id=assistants[-1].id,
    )
    assert frozen.covered_message_ids[-1] == assistants[-1].id
