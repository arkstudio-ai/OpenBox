"""A run that keeps sending the model a prompt it has already sent is stopped.

Replays the provider-reported input tokens of every model call on 2026-09-14 in
session_7YBXNNJ7KGM2YPWK39MXAJZXCF. The calls before 09:30:34 Beijing time are
a healthy video-production turn: the prompt grows with every tool result. At
09:30:34 the session wrote its 201st message, the loop kept loading the first
200, and for 140 calls the count alternated between two values while the model
re-asked a question the user had already answered, again and again.
"""
from agent.context_stall import ContextStallDetector

# UTC time and input tokens of each model call, in order.
_INCIDENT = """
01:08:39 174317  01:09:25 175840  01:11:39 177549  01:12:32 179243  01:12:44 182858  01:12:55 183518
01:13:02 183579  01:13:11 184254  01:14:03 184913  01:14:15 185275  01:14:22 185683  01:14:32 185843
01:14:40 186134  01:14:51 186433  01:15:01 186729  01:15:13 187023  01:15:27 187906  01:16:12 189114
01:16:25 189699  01:16:37 190556  01:16:46 190869  01:16:59 191145  01:17:15 191433  01:17:25 191715
01:17:32 191997  01:17:42 192182  01:18:15 192373  01:18:26 192596  01:18:33 192819  01:18:43 193042
01:18:53 193265  01:19:03 193450  01:19:10 193641  01:19:22 193827  01:19:31 194017  01:19:40 194240
01:19:51 194708  01:20:02 195198  01:20:11 195669  01:20:20 196152  01:20:29 196662  01:20:41 197201
01:20:50 197926  01:20:57 198521  01:21:05 199007  01:21:15 199413  01:25:26 200150  01:25:37 200440
01:25:52 201235  01:26:00 205708  01:26:11 205941  01:26:24 206046  01:30:20 206559  01:30:32 207710
01:30:40 206987  01:30:49 206987  01:31:14 207883  01:31:28 207883  01:31:41 207883  01:31:50 206987
01:31:58 206987  01:32:13 207883  01:32:26 207883  01:32:35 206987  01:34:46 206987  01:34:55 206987
01:35:08 207883  01:35:21 207883  01:35:34 207883  01:35:43 206987  01:35:51 206987  01:36:04 207883
01:36:12 206987  01:36:26 207883  01:36:43 207883  01:36:51 206987  01:36:58 206987  01:37:11 207883
01:37:27 207883  01:37:38 206987  01:37:46 206987  01:38:02 207883  01:38:14 207883  01:38:23 206987
01:38:31 206987  01:38:44 207883  01:38:52 206987  01:39:05 207883  01:39:13 206987  01:39:24 206987
01:39:35 207883  01:39:43 206987  01:39:57 207883  01:40:06 206987  01:40:16 206987  01:40:24 206987
01:40:32 206987  01:40:46 207883  01:40:55 206987  01:41:02 206987  01:41:11 206987  01:42:46 206987
01:42:55 206987  01:43:05 206987  01:43:15 206987  01:43:24 206987  01:43:33 206987  01:43:46 207883
01:43:54 206987  01:44:02 206987  01:44:18 207883  01:44:27 206987  01:44:36 206987  01:44:50 206987
01:44:59 206987  01:45:08 206987  01:45:21 207883  01:45:34 207883  01:45:48 207883  01:45:57 206987
01:46:19 207883  01:46:30 206987  01:46:42 207883  01:46:56 207883  01:47:05 206987  01:47:13 206987
01:47:25 207883  01:47:35 206987  01:47:42 206987  01:47:51 206987  01:48:04 207883  01:48:13 206987
01:48:22 206987  01:48:31 206987  01:48:39 206987  01:48:52 207883  01:49:03 206987  01:49:16 207883
01:49:28 206987  01:49:37 206987  01:49:48 207883  01:49:59 207883  01:50:12 207883  01:50:24 207883
01:50:38 207883  01:50:45 206987  01:50:55 206987  01:51:11 207883  01:51:27 207883  01:51:38 206987
01:51:48 206987  01:51:59 207883  01:52:11 207883  01:52:19 206987  01:52:29 206987  01:52:37 206987
01:52:51 207883  01:52:58 206987  01:53:06 206987  01:53:15 206987  01:53:26 207883  01:53:39 207883
01:53:47 206987  01:54:02 207883  01:54:17 207883  01:54:29 207883  01:54:42 207883  01:54:53 207883
01:55:05 207883  01:55:14 206987  01:59:00 207883  01:59:08 206987  01:59:18 207883  01:59:27 206987
01:59:39 207883  01:59:52 207883  02:00:05 207883  02:00:17 206987  02:00:25 206987  02:00:39 207883
02:00:56 207883  02:01:06 206987  02:01:22 207883  02:01:37 207883  02:01:49 207883  02:02:29 207883
02:02:38 206987  02:07:23 207883  02:07:34 206987  02:07:42 206987  02:07:51 206987  02:08:05 207883
02:08:19 206987  02:08:30 206987
"""
CALLS = [(time, int(tokens)) for time, tokens in zip(*[iter(_INCIDENT.split())] * 2)]

#: When message 201 was written; every call from here on saw the same prompt.
FROZEN_FROM = "01:30:34"
#: Each answer resumed the session as a new run with its own step counter.
RUN_STARTS = ["01:30:20", "01:31:14", "01:34:46", "01:42:46", "01:59:00", "02:07:23"]


def _replay(calls, detector=None):
    detector = detector or ContextStallDetector()
    for time, tokens in calls:
        if detector.observe(tokens):
            return time
    return None


def test_the_incident_is_stopped_within_five_minutes_of_freezing():
    stopped_at = _replay(CALLS)
    assert stopped_at is not None
    assert FROZEN_FROM < stopped_at <= "01:35:30", stopped_at


def test_the_healthy_turn_before_it_never_trips():
    healthy = [call for call in CALLS if call[0] < FROZEN_FROM]
    assert len(healthy) == 54
    assert _replay(healthy) is None


def test_a_resumed_run_picks_the_streak_up_from_seeded_steps():
    # Seeded with the session's last steps, each new run stops on the same call
    # one uninterrupted detector would have: the answers never reached the
    # model, so they did not change what it was sent.
    stopped_at = None
    for index, start in enumerate(RUN_STARTS):
        end = RUN_STARTS[index + 1] if index + 1 < len(RUN_STARTS) else "99:99:99"
        detector = ContextStallDetector()
        for _, tokens in [call for call in CALLS if call[0] < start][-detector.window:]:
            detector.observe(tokens)
        stopped_at = _replay([call for call in CALLS if start <= call[0] < end], detector)
        if stopped_at:
            break
    assert stopped_at == _replay(CALLS)


def test_growth_and_a_compaction_drop_never_trip():
    detector = ContextStallDetector()
    before = [180_000 + 900 * step for step in range(40)]
    after = [42_000 + 900 * step for step in range(40)]
    assert _replay([("", tokens) for tokens in before + after], detector) is None


def test_isolated_repeats_do_not_add_up():
    # A retried step can re-send its prompt once; that is not a frozen history.
    tokens = []
    for step in range(40):
        tokens += [150_000 + 700 * step] * 2
    assert _replay([("", value) for value in tokens]) is None


def test_steps_without_usage_neither_count_nor_reset():
    detector = ContextStallDetector(threshold=3)
    assert not detector.observe(5_000)
    assert not detector.observe(5_000)
    assert not detector.observe(0)
    assert not detector.observe(None)
    assert not detector.observe(5_000)
    assert detector.observe(5_000)


async def test_the_loop_stops_a_run_that_keeps_resending_one_prompt(monkeypatch):
    # Wiring, not arithmetic: steps that end in tool_calls with the same input
    # size every time must end the run after STALL_STEPS repeats, with an error
    # the chat can show, instead of calling the model until max_steps.
    import asyncio
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from sqlalchemy import text

    from agent import loop, processor
    from agent.context_stall import STALL_STEPS
    from core.config import get_config
    from bus.events import SESSION_ERROR
    from db.base import get_db_session
    from db.repository.user_repo import PgUserRepo
    from sandbox import sandbox_manager
    from sandbox.entitlement import SandboxSubscriptionRequired
    from session.session import create_session, create_user_message

    # This fixture replays a stalled provider usage count, not actual oversized
    # input. Keep automatic compaction independent of this stall-detector test.
    config = get_config().model_copy(deep=True)
    config.compaction.auto = False
    monkeypatch.setattr("core.config.get_config", lambda: config)

    # kv_store is migration-owned, not part of ORM create_all in the fixture.
    async with get_db_session() as db:
        await db.execute(text("CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"))
    user = await PgUserRepo().create(id=uuid4().hex, username=uuid4().hex, password_hash="test")
    session = await create_session(user_id=user["id"], title="Stalled", model="gpt-5.6-luna")
    await create_user_message(session_id=session.id, text="字幕重新配一下", agent="build",
                              model=session.model, user_id=user["id"])
    monkeypatch.setattr(sandbox_manager, "get_client", AsyncMock(side_effect=SandboxSubscriptionRequired()))
    monkeypatch.setattr(loop, "_ensure_title", AsyncMock())
    events = []
    monkeypatch.setattr("bus.bus.publish", lambda event, data: events.append((event, data)))
    calls = 0

    async def stream(**kwargs):
        nonlocal calls
        calls += 1
        yield {"type": "text_delta", "text": "再分析一下音频。"}
        yield {"type": "finish", "reason": "tool_calls", "usage": {"input": 206_987, "output": 200}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    await loop.run_loop(session.id, user_id=user["id"])
    if loop._background_tasks:
        await asyncio.gather(*list(loop._background_tasks))

    assert calls == STALL_STEPS + 1
    errors = [data["error"] for event, data in events if event == SESSION_ERROR]
    assert [error["code"] for error in errors] == ["CONTEXT_STALLED"]
