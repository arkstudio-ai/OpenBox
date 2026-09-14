"""Execution fixes: released stale leases, one terminal fact per run, isolated sweeps.

Also the after-commit contract of runtime facts and the effects of releasing a
superseded run's lease on notifications and late job results. Uses the
migration-backed durable question schema (PostgreSQL multi-worker mode too).
"""
import asyncio
import itertools
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import event, update
from sqlalchemy.orm import Session as SyncSession

import db.base as database
from db.models.message import Message
from db.models.part import Part
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session
from db.models.video_job import VideoJob
from question import question as q
from question import runtime
from question.continuation import QuestionContinuationWorker, apply_answers, expire_questions
from session.session import create_assistant_message, create_user_message, delete_messages_from, delete_session
from tests.unit.test_durable_questions import checkpoint, read, state  # noqa: F401
from tests.unit.test_run_fencing_api import (  # noqa: F401
    acting_as,
    add_session,
    expire_lease,
    loop_harness,
    published,
    recording,
    trajectory_events,
)

PAST = timedelta(seconds=1)
OPERATIONS = ("cancel", "invalidate", "finish", "recover", "delete")


async def ask_in(session_id: str, *, expires_at=None) -> str:
    """A pending durable question in a session of its own."""
    await add_session(session_id)
    message_id, part_id = f"m-{session_id}", f"p-{session_id}"
    async with database.get_db_session() as db:
        db.add(Message(id=message_id, session_id=session_id, user_id="u1", role="assistant",
                       finish="waiting_input", created_at=runtime.now()))
        await db.flush()
        db.add(Part(id=part_id, session_id=session_id, message_id=message_id, user_id="u1", type="tool",
                    data={"id": part_id, "type": "tool", "tool": "question", "status": "running"},
                    created_at=runtime.now()))
    with pytest.raises(q.QuestionSuspended) as suspended:
        await q.ask(session_id, [q.Question(question="Choose?", options=[q.QuestionOption(label="Yes")])],
                    {"messageID": message_id, "callID": part_id}, "u1", expires_at=expires_at)
    return suspended.value.request_id


def resumed_runs(monkeypatch) -> list:
    """Replace the resumed agent loop with a run that starts and finishes at once."""
    started = []

    async def run(session_id, user_id, expected_generation):
        ticket = await runtime.start_run(session_id, user_id, expected_generation=expected_generation)
        started.append(session_id)
        if ticket:
            await runtime.finish_run(ticket)
    monkeypatch.setattr("agent.loop.run_loop", run)
    return started


async def sweep(times: int = 1) -> None:
    worker = QuestionContinuationWorker()
    try:
        for _ in range(times):
            await worker.tick()
            await asyncio.gather(*worker.runs.values())
    finally:
        await worker.stop()


async def test_stop_then_lease_expiry_keeps_the_session_usable_and_every_sweep_running(
        state, recording, monkeypatch):
    from session.abort import abort_session_turn
    monkeypatch.setattr("session.abort._ABORT_SETTLE_SECONDS", 0)
    monkeypatch.setattr("sandbox.sandbox_manager.release", AsyncMock())
    started = resumed_runs(monkeypatch)
    prompt = await create_user_message("s1", "A long task", user_id="u1")
    ticket = await runtime.start_run("s1", "u1")
    with acting_as(ticket):
        await create_assistant_message("s1", prompt.id, user_id="u1")
    assert await abort_session_turn("s1", "u1", reason="user_stop")
    # The stopped run unwinds late; its finish no longer owns the session.
    await runtime.finish_run(ticket, completed=True)
    expiring = await ask_in("s3", expires_at=runtime.now() - PAST)
    answered = await ask_in("s4")
    await q.reply(answered, [["Yes"]], "u1")
    await expire_lease("s1")

    await sweep(times=2)
    assert (await read(SessionExecution, "s1")).run_id is None
    assert (await read(Session, "s1")).status == "idle"
    assert not published(state, "session.error")
    assert (await read(QuestionCheckpoint, expiring)).status == "expired"
    assert started == ["s4"]

    # The session stays usable: prompt, regenerate, a second stop and delete.
    retry = await create_user_message("s1", "Try again", user_id="u1")
    rerun = await runtime.start_run("s1", "u1")
    assert rerun is not None
    with acting_as(rerun):
        reply = await create_assistant_message("s1", retry.id, user_id="u1")
    await runtime.finish_run(rerun, completed=True)
    assert await delete_messages_from("s1", reply.id, user_id="u1") == retry.id
    await abort_session_turn("s1", "u1", reason="user_stop", was_active=False)
    await abort_session_turn("s1", "u1", reason="user_stop", was_active=False)
    assert await delete_session("s1", "u1")


async def test_stop_then_immediate_send_runs_the_new_turn_untouched_by_the_stopped_run(
        state, recording, loop_harness, monkeypatch):
    from session import status as run_status
    from session.abort import abort_session_turn
    monkeypatch.setattr("session.abort._ABORT_SETTLE_SECONDS", 0)
    tickets = []
    real_start = runtime.start_run

    async def start(*args, **kwargs):
        ticket = await real_start(*args, **kwargs)
        tickets.append(ticket)
        return ticket

    async def no_suggestions(*args, **kwargs):
        return None
    monkeypatch.setattr(runtime, "start_run", start)
    monkeypatch.setattr("agent.suggestions.generate_suggestions", no_suggestions)
    streaming = asyncio.Event()
    requests = []

    async def provider(**kwargs):
        requests.append(kwargs["model_id"])
        if len(requests) == 1:
            yield {"type": "text_delta", "text": "Working on the long task"}
            streaming.set()
            await asyncio.Event().wait()  # Only the stop ends this response.
        yield {"type": "text_delta", "text": "Here is the new answer"}
        yield {"type": "finish", "reason": "stop", "usage": {}}
    monkeypatch.setattr(loop_harness.processor, "stream_llm", provider)

    await create_user_message("s1", "A long task", user_id="u1")
    stopped = asyncio.create_task(loop_harness.loop.run_loop("s1", user_id="u1"))
    await asyncio.wait_for(streaming.wait(), timeout=10)
    assert await abort_session_turn("s1", "u1", reason="user_stop")
    # The user sends again at once, while the stopped run may still be unwinding.
    await create_user_message("s1", "Something else instead", user_id="u1")
    answer = await asyncio.wait_for(loop_harness.loop.run_loop("s1", user_id="u1"), timeout=10)
    await asyncio.wait_for(stopped, timeout=10)

    assert answer is not None and len(tickets) == 2
    first, second = tickets
    assert runtime.is_revoked(first.run_id) and not runtime.is_revoked(second.run_id)
    assert (await read(SessionExecution, "s1")).run_id is None
    assert (await read(Session, "s1")).status == "idle"
    assert not published(state, "session.error")
    statuses = [data["status"] for data in published(state, "session.status") if data["sessionId"] == "s1"]
    assert statuses[-1] == "idle" and "error" not in statuses
    # Neither run leaves an abort signal behind for a later run to trip over.
    assert first.run_id not in run_status._run_signals and second.run_id not in run_status._run_signals
    assert "s1" not in run_status._abort_signals and "s1" not in run_status._pending_aborts


async def _apply(operation: str, session_id: str, ticket) -> None:
    if operation == "cancel":
        await runtime.cancel_session(session_id, "u1")
    elif operation == "invalidate":
        await create_user_message(session_id, "Replacement", user_id="u1")
    elif operation == "finish":
        await runtime.finish_run(ticket, completed=True)
    elif operation == "recover":
        await expire_lease(session_id)
        await runtime.recover_expired_runs()
    else:
        assert await delete_session(session_id, "u1")


async def test_every_ordering_records_exactly_one_terminal_fact_per_run(state, recording, monkeypatch):
    import trajectory
    monkeypatch.setattr("sandbox.sandbox_manager.release", AsyncMock())
    original = trajectory.record
    terminals = []

    async def counted(kind, data, **kwargs):
        if kind in {"run.finished", "run.interrupted"}:
            terminals.append(kwargs["event_id"])
        return await original(kind, data, **kwargs)
    monkeypatch.setattr(trajectory, "record", counted)
    for index, order in enumerate(itertools.permutations(OPERATIONS)):
        session_id = f"order-{index}"
        await add_session(session_id)
        await create_user_message(session_id, "Work", user_id="u1")
        ticket = await runtime.start_run(session_id, "u1")
        deleted = False
        for operation in order:
            try:
                await _apply(operation, session_id, ticket)
            except (LookupError, ValueError):
                # Only a deleted session refuses the operations that follow.
                assert deleted, (order, operation)
            deleted = deleted or operation == "delete"
        facts = [event_id for event_id in terminals if event_id.endswith(ticket.run_id)]
        assert len(facts) == (1 if recording else 0), (order, facts)
        assert (await read(SessionExecution, session_id)).run_id is None, order


async def test_poisoned_lease_is_released_quietly_by_recovery_and_by_new_input(state, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    await create_user_message("s1", "Work", user_id="u1")
    ticket = await runtime.start_run("s1", "u1")
    await runtime.cancel_session("s1", "u1")
    assert len(await _terminal_facts(ticket.run_id)) == 1

    async def poison(lease_until):
        # Rows stopped before this release kept the lease after invalidation, and
        # a restarted process remembers no terminal fact recorded for them.
        monkeypatch.setattr(runtime, "_terminal_runs", runtime._Recent())
        async with database.get_db_session() as db:
            await db.execute(update(SessionExecution).where(SessionExecution.session_id == "s1").values(
                run_id=ticket.run_id, run_generation=ticket.generation, lease_until=lease_until))

    await poison(runtime.now() - PAST)
    await runtime.recover_expired_runs()
    execution = await read(SessionExecution, "s1")
    assert execution.run_id is None and execution.resume_error is None
    assert (await read(Session, "s1")).status == "idle"
    assert not published(state, "session.error")
    assert len(await _terminal_facts(ticket.run_id)) == 1

    await poison(runtime.now() + timedelta(seconds=60))
    await create_user_message("s1", "Next request", user_id="u1")
    assert (await read(SessionExecution, "s1")).run_id is None
    assert len(await _terminal_facts(ticket.run_id)) == 1
    assert await runtime.start_run("s1", "u1") is not None


async def _terminal_facts(run_id: str) -> list:
    events = await trajectory_events("run.finished", "run.interrupted")
    return [event for event in events if event.context.get("run_id") == run_id]


async def test_recovery_isolates_a_failing_candidate(state, monkeypatch):
    await add_session("s3")
    failing = await runtime.start_run("s1", "u1")
    healthy = await runtime.start_run("s3", "u1")
    await expire_lease("s1")
    await expire_lease("s3")
    real = runtime.waiting_status

    async def broken(db, execution, fallback="idle"):
        if execution.session_id == "s1":
            raise RuntimeError("injected recovery failure")
        return await real(db, execution, fallback)
    monkeypatch.setattr(runtime, "waiting_status", broken)
    await runtime.recover_expired_runs()
    assert (await read(SessionExecution, "s1")).run_id == failing.run_id
    assert (await read(SessionExecution, "s3")).run_id is None
    assert (await read(Session, "s3")).status == "error"
    assert healthy.run_id != failing.run_id


async def test_question_expiry_isolates_each_session(state, monkeypatch):
    import question.question as questions
    broken = await ask_in("s3", expires_at=runtime.now() - PAST)
    healthy = await ask_in("s4", expires_at=runtime.now() - PAST)
    real = questions.record_checkpoint

    async def failing(db, row, execution, event_type, data=None):
        if row.session_id == "s3":
            raise RuntimeError("injected expiry failure")
        return await real(db, row, execution, event_type, data)
    monkeypatch.setattr(questions, "record_checkpoint", failing)
    await expire_questions()
    assert (await read(QuestionCheckpoint, broken)).status == "pending"
    assert (await read(QuestionCheckpoint, healthy)).status == "expired"


@pytest.mark.parametrize("failing_phase", ["lease recovery", "question expiry"])
async def test_tick_phases_run_even_when_another_phase_fails(state, monkeypatch, failing_phase):
    expiring = await ask_in("s3", expires_at=runtime.now() - PAST)
    answered = await ask_in("s4")
    await q.reply(answered, [["Yes"]], "u1")
    started = resumed_runs(monkeypatch)

    async def broken():
        raise RuntimeError(f"injected {failing_phase} failure")
    if failing_phase == "lease recovery":
        monkeypatch.setattr(runtime, "recover_expired_runs", broken)
    else:
        monkeypatch.setattr("question.continuation.expire_questions", broken)
    await sweep()
    assert started == ["s4"]
    expected = "pending" if failing_phase == "question expiry" else "expired"
    assert (await read(QuestionCheckpoint, expiring)).status == expected


async def test_recording_failure_while_applying_answers_is_retried_not_a_resume_failure(state, monkeypatch):
    import trajectory
    from trajectory.types import RecordingError
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    request_id = await checkpoint()
    await q.reply(request_id, [["Yes"]], "u1")
    original = trajectory.record

    async def failing(kind, data, **kwargs):
        if kind == "input.injected":
            raise RecordingError("injected journal failure")
        return await original(kind, data, **kwargs)
    monkeypatch.setattr(trajectory, "record", failing)
    await sweep()
    execution = await read(SessionExecution, "s1")
    assert execution.resume_pending and execution.next_attempt_at is not None
    assert execution.resume_error is None
    assert not published(state, "session.error")
    assert not (await read(QuestionCheckpoint, request_id)).applied


@pytest.mark.parametrize("restarted", [False, True], ids=["same-process", "after-restart"])
@pytest.mark.parametrize("second_run", ["regenerate", "plan_accept"])
async def test_two_completed_runs_in_one_turn_finish_cleanly_with_one_turn_finished(
        state, monkeypatch, second_run, restarted):
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    prompt = await create_user_message("s1", "Write a plan", user_id="u1")
    first = await runtime.start_run("s1", "u1")
    with acting_as(first):
        reply = await create_assistant_message("s1", prompt.id, user_id="u1")
    await runtime.finish_run(first, completed=True)
    if restarted:
        # A deploy between the two runs: this process remembers no earlier fact
        # of the turn, while the legacy sink still holds turn_finish for it.
        monkeypatch.setattr(runtime, "_terminal_runs", runtime._Recent())
        monkeypatch.setattr(runtime, "_finished_turns", runtime._Recent())
    if second_run == "regenerate":
        await delete_messages_from("s1", reply.id, user_id="u1")
    else:
        await create_user_message("s1", "The plan has been approved. Execute the plan",
                                  synthetic=True, user_id="u1")
    second = await runtime.start_run("s1", "u1")
    await runtime.finish_run(second, completed=True)

    assert (await read(SessionExecution, "s1")).run_id is None
    assert (await read(Session, "s1")).status == "idle"
    assert not published(state, "session.error")
    finished_runs = await trajectory_events("run.finished")
    assert {event.context["run_id"] for event in finished_runs} == {first.run_id, second.run_id}
    assert {event.context["turn_id"] for event in finished_runs} == {prompt.id}
    assert len(await trajectory_events("turn.finished")) == 1


class AfterCommitEmitter:
    """Stands in for the spool emitter: a fact leaves only when its transaction commits."""

    def __init__(self, *, available: bool):
        self.available = available
        self.calls: list[tuple[str, bool]] = []
        self.delivered: list[str] = []

    async def record(self, kind, data, *, context=None, db=None, **ids):
        self.calls.append((kind, db is not None))
        if context is None or not self.available:
            return None  # An unavailable emitter drops the fact and never raises.
        if db is None:
            self.delivered.append(kind)
        else:
            db.sync_session.info.setdefault("fixture_pending_facts", []).append(kind)
        return None

    def committed(self, session):
        if not session.in_nested_transaction():
            self.delivered.extend(session.info.pop("fixture_pending_facts", ()))

    def rolled_back(self, session, previous_transaction):
        if not previous_transaction.nested:
            session.info.pop("fixture_pending_facts", None)


@pytest.fixture(params=[True, False], ids=["emitter-available", "emitter-failing"])
def emitter(request, state, monkeypatch):
    import trajectory
    sink = AfterCommitEmitter(available=request.param)
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    monkeypatch.setattr(trajectory, "record", sink.record)
    event.listen(SyncSession, "after_commit", sink.committed)
    event.listen(SyncSession, "after_soft_rollback", sink.rolled_back)
    yield sink
    event.remove(SyncSession, "after_commit", sink.committed)
    event.remove(SyncSession, "after_soft_rollback", sink.rolled_back)


RUNTIME_FACTS = {"turn.started", "run.started", "run.finished", "run.interrupted", "run.cancel_requested",
                 "turn.finished", "question.asked", "question.resolved", "question.cancelled",
                 "part.committed", "tool.finished", "input.injected"}


async def test_transitions_emit_only_committed_facts_and_ignore_emitter_failures(state, emitter):
    # ask, reply and apply
    request_id = await checkpoint()
    await q.reply(request_id, [["Yes"]], "u1")
    assert await apply_answers("s1", "u1") == 0
    assert (await read(Part, "p1")).data["status"] == "completed"
    # start and finish
    resumed = await runtime.start_run("s1", "u1", expected_generation=0)
    await runtime.finish_run(resumed, completed=True)
    assert (await read(Session, "s1")).status == "idle"
    # recover, then cancel
    await runtime.start_run("s1", "u1")
    await expire_lease()
    await runtime.recover_expired_runs()
    assert (await read(Session, "s1")).status == "error"
    await runtime.start_run("s1", "u1")
    await runtime.cancel_session("s1", "u1")
    execution = await read(SessionExecution, "s1")
    assert execution.run_id is None and execution.generation == 1

    # A rolled back invalidation discards the facts it produced.
    pending = await checkpoint(part_id="p2")
    delivered = list(emitter.delivered)
    with pytest.raises(RuntimeError):
        async with runtime.transaction("s1", "u1", fence=False) as (db, _, execution):
            await runtime.invalidate_locked(db, execution)
            raise RuntimeError("message insert failed")
    assert (await read(QuestionCheckpoint, pending)).status == "pending"
    assert emitter.delivered == delivered

    runtime_calls = [with_db for kind, with_db in emitter.calls if kind in RUNTIME_FACTS]
    assert runtime_calls and all(runtime_calls)  # Every transition fact is bound to its transaction.
    if emitter.available:
        assert {"question.asked", "question.resolved", "input.injected", "run.started", "run.finished",
                "run.interrupted", "run.cancel_requested"} <= set(emitter.delivered)
        assert "question.cancelled" not in emitter.delivered
    else:
        assert emitter.delivered == []


async def test_superseded_run_gets_no_auth_prompt_and_its_late_job_result_is_flagged(state, monkeypatch):
    from notifications import events
    from trajectory.jobs import record_job_in_tx
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    prompts = []

    async def emit(db, **fields):
        prompts.append(fields["kind"])
    monkeypatch.setattr(events, "emit", emit)
    account = SimpleNamespace(id=uuid4().hex, workspace_id="w1", platform="douyin")
    await create_user_message("s1", "Publish the clip", user_id="u1")
    ticket = await runtime.start_run("s1", "u1")
    submitted = await runtime.get_run_trace(ticket)
    job_id = uuid4().hex
    async with database.get_db_session() as db:
        await events.auth_blocked(db, account, session_id="s1", user_id="u1")
        db.add(VideoJob(id=job_id, user_id="u1", session_id="s1", kind="segment", idempotency_key=job_id,
                        status="in_progress", model="fixture", attempt=1,
                        request_data={"_trajectory_context": submitted.to_dict()}, result_data={},
                        created_at=runtime.now(), updated_at=runtime.now()))
    assert prompts == ["platform_auth_expired"]

    await create_user_message("s1", "Never mind", user_id="u1")
    async with database.get_db_session() as db:
        await events.auth_blocked(db, account, session_id="s1", user_id="u1")
    assert prompts == ["platform_auth_expired"]  # The superseded run is not interrupted for auth.
    async with database.get_db_session() as db:
        job = await db.get(VideoJob, job_id)
        job.status = "completed"
        await record_job_in_tx(db, job)
    late = await trajectory_events("operation.late_result")
    assert [event.data["original_run_id"] for event in late] == [ticket.run_id]
