"""A current card answer is action provenance, even with history recall off."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from assistant.projection import project_main_messages
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from question import question as q
from session.agent_event_log import load_canonical_model_surface
from session.session import create_assistant_message
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_placement import quiet  # noqa: F401
from tests.unit.test_assistant_pending import saved_request, pause
from tests.unit.test_assistant_sessions_v2 import main_turn, card, tool_call
from tool.tool import ToolContext


async def answered_choice(detached=True, kind="placement"):
    if kind == "model":
        from tests.unit.test_assistant_placement import prepare
        owner, workspace, main, ctx, lease, arguments = await prepare()
        arguments = {**arguments, "model_confirmed": False, "destination_confirmed": True}
        asked, _ = await tool_call(ctx, "tasks.submit", arguments)
        identity = asked["suspended"]
        await pause(ctx, lease)
    else:
        owner, workspace, main, arguments, identity, _ = await saved_request()
    # The user changed topic while the preparation choice remained pending.
    if detached:
        ctx, lease, _ = await main_turn(owner, workspace, main, "先聊别的")
        await pause(ctx, lease)
    row = await card(identity)
    choice = row.questions[0]["options"][0]["label"]
    assert (await q.reply(identity, [[choice]], owner))["applied"]
    if not detached:
        from question.continuation import QuestionContinuationWorker
        await QuestionContinuationWorker()._resume_candidate(main.id, owner, row.generation)
    lease = await reserve_run(main.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
        agent="assistant", user_id=owner, run_fence=(main.id, lease.run_id, lease.generation))
    ctx = ToolContext(user_id=owner, workspace_id=workspace, session_id=main.id, project_id=main.project_id,
        agent_id="assistant", run_id=lease.run_id, run_generation=lease.generation, message_id=message.id)
    return ctx, lease, arguments, identity, choice


async def project(ctx, **kwargs):
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    # The semantic current-only path has already stripped the prior transcript.
    messages = list(surface.messages)
    start = max(i for i, m in enumerate(messages) if m.role == "user")
    return await project_main_messages(messages[start:], ctx=ctx, **kwargs)


@pytest.mark.parametrize("policy", ["allowed", "current_only", "no_history", "uncertain"])
@pytest.mark.parametrize("detached", [True, False])
@pytest.mark.parametrize("kind", ["placement", "model"])
async def test_bound_answer_survives_semantic_recall_and_continues_original_request(monkeypatch, policy, detached, kind):
    ctx, lease, arguments, identity, choice = await answered_choice(detached, kind)
    from core.config import get_config
    config = get_config().model_copy(deep=True)
    config.memory.semantic_recall = True
    config.memory.allowed_user_ids = []
    monkeypatch.setattr("core.config.get_config", lambda: config)
    ctx.memory_turn = SimpleNamespace(read_policy=policy, explicit_read=False)
    try:
        messages = await project(ctx)
        [block] = [m for m in messages if m.id == "assistant:answered-card"]
        payload = json.loads(block.parts[0]["text"].split("\n", 1)[1])
        assert len(payload["items"]) == 1
        record = payload["items"][0]
        assert record["request_id"] == identity and record["answers"] == [[choice]]
        assert record["original_call"] == {"tool": "tasks.submit", "arguments": arguments}
        assert record["execution_confirmed"] is False
        assert not {m.id for m in messages} & {"assistant:pending-decisions", "assistant:work-directory", "assistant:recent-call"}
        assert not any(m.id == block.id for m in await project(ctx, for_compaction=True))
        # The supplied arguments go through the real admission path, not a replay bypass.
        result, _ = await tool_call(ctx, "tasks.submit", record["original_call"]["arguments"])
        assert result["state"] == "accepted"
        assert (await tool_call(ctx, "tasks.submit", arguments))[0] == result
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["missing_id", "unknown_id", "human_origin", "old_generation"])
async def test_unbound_or_forged_receipt_never_reads_another_card(monkeypatch, change):
    ctx, lease, _, identity, _ = await answered_choice()
    from core.config import get_config
    config = get_config().model_copy(deep=True)
    config.memory.semantic_recall = True
    config.memory.allowed_user_ids = []
    monkeypatch.setattr("core.config.get_config", lambda: config)
    ctx.memory_turn = SimpleNamespace(read_policy="no_history", explicit_read=False)
    try:
        async with get_db_session() as db:
            row = await db.scalar(select(AgentInboxItem).where(AgentInboxItem.run_id == ctx.run_id))
            if change == "missing_id": row.origin_ref = {"entrypoint": "assistant_card_answer"}
            elif change == "unknown_id": row.origin_ref = {**row.origin_ref, "question_id": "another-card"}
            elif change == "human_origin": row.origin = "human"
            else: row.generation -= 1
        assert not any(m.id == "assistant:answered-card" for m in await project(ctx))
    finally:
        await lease.release(session_status="idle")
