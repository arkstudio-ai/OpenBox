"""The assistant's one conversation folds old history into a rolling summary (V2 P3b).

docs/PERSONAL_ASSISTANT_DESIGN_V2.md 8.6: there is one assistant conversation
for good. After an ordinary turn has answered, history beyond a bound of
model-visible messages is summarized, keeping the newest turns verbatim. The
answer is never at stake: the summary does not count against the turn's
budget, and a failed summary leaves the turn's outcome and the next turn intact.
"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from agent import inbox, loop, processor
from agent.driver import reserve_run
from assistant.service import ensure_main_session
from core.config import get_config
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from session.agent_event_log import load_canonical_model_surface, verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tool.assistant_tools import assistant_tools


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(get_config(), "jwt_secret", "assistant-rolling-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    config = _loop_config()
    config.permission = {"*": "allow"}
    config.compaction.auto = True
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)

    async def tools(*_args, **_kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools}, catalogue_availability="available")

    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    monkeypatch.setattr(loop, "ASSISTANT_ROLLING_MESSAGES", 6)
    monkeypatch.setattr(loop, "ASSISTANT_ROLLING_TAIL_TURNS", 2)
    payloads, summaries = [], []

    async def answer(**kwargs):
        payloads.append(json.dumps(kwargs["messages"], ensure_ascii=False))
        yield {"type": "text_delta", "text": f"ANSWER_{len(payloads)}"}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    state = SimpleNamespace(fail=False)

    async def summarize(**kwargs):
        summaries.append(json.dumps(kwargs["messages"], ensure_ascii=False))
        if state.fail:
            yield {"type": "error", "error": "provider down"}
            return
        yield {"type": "text_delta", "text": f"ROLLING_SUMMARY_{len(summaries)}: earlier turns covered."}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", answer)
    monkeypatch.setattr("agent.llm.stream_llm", summarize)
    return SimpleNamespace(config=config, payloads=payloads, summaries=summaries, state=state)


async def human_turn(main, owner, text):
    item = await inbox.accept_inbox_item(session_id=main.id, user_id=owner, delivery="followup", prompt=text,
        agent="assistant", origin="human", origin_ref={"actor_user_id": owner})
    lease = await reserve_run(main.id, owner)
    try:
        await loop.run_loop(main.id, user_id=owner, lease=lease)
    finally:
        await lease.release(session_status="idle")
    async with get_db_session() as db:
        return await db.get(AgentInboxItem, item["id"] if isinstance(item, dict) else item.id)


async def count(main, kind, **filters):
    async with get_db_session() as db:
        return int(await db.scalar(select(func.count()).select_from(AgentEvent).where(
            AgentEvent.session_id == main.id, AgentEvent.kind == kind,
            *(getattr(AgentEvent, key) == value for key, value in filters.items()))))


async def main_session(runtime):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=runtime.config.model)
    return owner, main


async def test_old_history_rolls_into_a_summary_after_the_answer(runtime):
    owner, main = await main_session(runtime)
    for index in range(3):
        settled = await human_turn(main, owner, f"OLDEST_{index} 第{index}个问题")
        assert (settled.state, settled.outcome) == ("settled", "succeeded")
    assert runtime.summaries == [] and await count(main, "surface.replacement") == 0
    settled = await human_turn(main, owner, "FOURTH 第四个问题")
    # The answer was saved and the turn succeeded; then history rolled.
    assert (settled.state, settled.outcome) == ("settled", "succeeded")
    assert len(runtime.summaries) == 1 and "OLDEST_0" in runtime.summaries[0]
    assert await count(main, "surface.replacement") == 1
    # The summary is maintenance: it is not one of the turn's model requests.
    assert len(runtime.payloads) == 4  # no further answer after the summary
    assert await count(main, "assistant.budget.request", turn_id=settled.message_id) == 1
    surface = await load_canonical_model_surface(main.id, user_id=owner)
    assert len(surface.messages) <= 2 + 2 * loop.ASSISTANT_ROLLING_TAIL_TURNS
    await human_turn(main, owner, "FIFTH 第五个问题")
    latest = runtime.payloads[-1]
    assert "ROLLING_SUMMARY_1" in latest and "FIFTH" in latest and "FOURTH" in latest
    assert "OLDEST_0" not in latest and "What did we do so far?" not in latest
    assert (await verify_agent_event_parity(main.id, user_id=owner)).ok


async def test_a_failed_summary_leaves_the_answer_and_the_next_turn_intact(runtime):
    owner, main = await main_session(runtime)
    runtime.state.fail = True
    for index in range(4):
        settled = await human_turn(main, owner, f"TURN_{index}")
        assert (settled.state, settled.outcome) == ("settled", "succeeded")
    assert runtime.summaries and await count(main, "surface.replacement") == 0
    runtime.state.fail = False
    await human_turn(main, owner, "AFTER_FAILURE 继续")
    latest = runtime.payloads[-1]
    assert "AFTER_FAILURE" in latest and "What did we do so far?" not in latest
    assert await count(main, "surface.replacement") == 1


async def test_decision_notes_keep_accumulating_without_breaking_turns(runtime, monkeypatch):
    """Months of notes: only the newest ride in each request, older ones are counted, nothing raises."""
    from assistant import decisions
    from session.agent_event_log import append_agent_event_locked, prepare_agent_event_write
    owner, main = await main_session(runtime)
    monkeypatch.setattr(decisions, "MAX_DECISION_EVENTS", 50)
    async with get_db_session() as db:
        row = await prepare_agent_event_write(db, session_id=main.id, user_id=owner, run_fence=None)
        for index in range(60):
            await append_agent_event_locked(db, row, kind=decisions.RECORDED, payload={
                "decision_id": f"note-{index:03d}", "task_id": None, "summary": f"NOTE_{index:03d}",
                "supersedes": [f"note-{index - 1:03d}"] if index == 59 else [],
                "created_at": "2026-10-06T00:00:00+00:00"})
        context = await decisions.decision_context(db, row, run_fence=(main.id, "run", 1))
    ids = [entry["decision_id"] for entry in context["decisions"]]
    # The newest 50 records are read; note-058 is superseded by note-059.
    assert len(ids) == decisions.MAX_CONTEXT_DECISIONS and ids[-1] == "note-059" and "note-058" not in ids
    assert context["older_notes"] == 49 - decisions.MAX_CONTEXT_DECISIONS
    settled = await human_turn(main, owner, "还在吗？")
    assert (settled.state, settled.outcome) == ("settled", "succeeded")
    assert "NOTE_059" in runtime.payloads[-1] and "NOTE_000" not in runtime.payloads[-1]
