"""Pending-plan revisions use real SQL, provenance and the durable input queue."""
from datetime import datetime, timezone
import asyncio

import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from assistant.history import read_history
from tests.unit.assistant_legacy_cards import legacy_send_cards
from assistant.requests import question_version
from core.identifier import generate_id
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask
from db.models.file_asset import FileAsset
from db.models.question import QuestionCheckpoint
from question import question as q
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_requests_v2 import ask_in
from tests.unit.test_assistant_sessions_v2 import (assistant, conversation, main_turn, tool_call,
                                                   quiet_runtime, human_turn)  # noqa: F401


async def setup(*, shared=False):
    owner, other, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "水果店视频", visibility="workspace" if shared else "private")
    request_id, _ = await ask_in(session, owner, question="确认30秒讲稿？", options=[{"label": "可以"}])
    asset_id = "asset_" + generate_id()
    async with get_db_session() as db:
        row = await db.get(QuestionCheckpoint, request_id)
        version = question_version(row)
        db.add(FileAsset(id=asset_id, user_id=owner, workspace_id=workspace, session_id=main.id,
            name="水果店.jpg", mime="image/jpeg", oss_key=f"assets/{owner}/fruit.jpg", size=20,
            status="ready", created_at=datetime.now(timezone.utc)))
    ctx, lease, human = await main_turn(owner, workspace, main, "讲稿可以，但改用这张水果店照片作为背景重新做。")
    args = {"request_id": request_id, "version": version,
        "instructions": "沿用30秒讲稿，改用选定水果店图片作为背景，重新规划视频，费用待新方案确认。",
        "attachment_ids": [asset_id], "source_message_ids": [human]}
    return owner, other, workspace, main, session, ctx, lease, args


@pytest.mark.parametrize("shared", [False, True])
async def test_revision_is_atomic_idempotent_and_runs_in_original_conversation(shared):
    owner, _, _, _, session, ctx, lease, args = await setup(shared=shared)
    try:
        first, _ = await tool_call(ctx, "requests.revise", args)
        assert first.get("state") == "accepted", first
        replay, _ = await tool_call(ctx, "requests.revise", args)
        assert replay == first
        async with get_db_session() as db:
            card = await db.get(QuestionCheckpoint, args["request_id"])
            item = await db.get(AgentInboxItem, first["inbox_id"])
            assert card.status == "superseded" and card.answers is None
            assert item.session_id == session.id and item.attachments == args["attachment_ids"]
            assert item.origin == "assistant_delegation" and item.prompt == args["instructions"]
            assert item.origin_ref["revision_request"]["request_id"] == card.id
            assert len((await db.scalars(select(AssistantTask).where(
                AssistantTask.execution_session_id == session.id))).all()) == 1
        with pytest.raises(q.QuestionGone):
            await q.reply(args["request_id"], [["可以"]], user_id=owner)
        execution_lease = await reserve_run(session.id, owner)
        try:
            batch = await inbox.claim_inbox_boundary(execution_lease, step=1, include_next_turn=True)
            assert batch.messages and batch.messages[0].id
            async with get_db_session() as db:
                item = await db.get(AgentInboxItem, first["inbox_id"])
                assert item.state == "claimed" and item.message_id == batch.messages[0].id
        finally:
            await execution_lease.release(session_status="idle")
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("failure", ["stale", "answered", "missing_asset", "foreign"])
async def test_failed_revision_preserves_card_and_creates_no_partial_link(failure):
    owner, other, workspace, main, session, ctx, lease, args = await setup()
    try:
        if failure == "stale": args["version"] = "0" * 64
        if failure == "answered": await q.reply(args["request_id"], [["可以"]], user_id=owner)
        if failure == "missing_asset": args["attachment_ids"] = ["asset_missing"]
        if failure == "foreign":
            foreign = await conversation(other, workspace, main, "别人的视频", visibility="private")
            args["request_id"], _ = await ask_in(foreign, other)
        async with get_db_session() as db:
            before = await db.scalar(select(QuestionCheckpoint.status).where(QuestionCheckpoint.session_id == session.id))
        result, _ = await tool_call(ctx, "requests.revise", args)
        assert result.get("error"), result
        async with get_db_session() as db:
            assert await db.scalar(select(QuestionCheckpoint.status).where(QuestionCheckpoint.session_id == session.id)) == before
            assert await db.scalar(select(AssistantTask.id).where(AssistantTask.execution_session_id == session.id)) is None
            assert not (await db.scalars(select(AgentInboxItem).where(
                AgentInboxItem.session_id == session.id, AgentInboxItem.origin == "assistant_delegation"))).all()
    finally:
        await lease.release(session_status="idle")


async def test_invalid_attachment_never_creates_a_shared_send_confirmation():
    owner, _, _, main, session, ctx, lease, args = await setup(shared=True)
    args["attachment_ids"] = [args["request_id"]]
    try:
        result, _ = await tool_call(ctx, "requests.revise", args)
        assert result.get("error") == "ASSISTANT_ATTACHMENT_UNAVAILABLE"
        async with get_db_session() as db:
            assert not (await db.scalars(select(QuestionCheckpoint).where(
                QuestionCheckpoint.session_id == main.id))).all()
            assert (await db.get(QuestionCheckpoint, args["request_id"])).status == "pending"
            assert await db.scalar(select(AssistantTask.id).where(
                AssistantTask.execution_session_id == session.id)) is None
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("changed", ["attachment_ids", "instructions", "request_id", "version"])
async def test_confirmation_resume_rejects_argument_drift_without_another_card(changed):
    owner, _, _, main, session, ctx, lease, args = await setup(shared=True)
    try:
        with legacy_send_cards():
            result, _ = await tool_call(ctx, "requests.revise", args)
        from assistant.confirmations import CONFIRM
        await q.reply(result["suspended"], [[CONFIRM]], user_id=owner)
        async with get_db_session() as db:
            card = await db.get(QuestionCheckpoint, result["suspended"])
            item = await db.scalar(select(AgentInboxItem).where(
                AgentInboxItem.run_id == ctx.run_id, AgentInboxItem.generation == ctx.run_generation))
            original_origin, original_ref = item.origin, dict(item.origin_ref)
            # The real continuation has this provenance, not a new human turn.
            item.origin = "system_recovery"
            item.origin_ref = {"entrypoint": "question_answer", "generation": card.generation}
        altered = {**args, changed: {"attachment_ids": [args["request_id"]], "instructions": "改成另一个方案",
                                    "request_id": "different-task", "version": "b" * 64}[changed]}
        from assistant.revisions import _check_confirmation_replay
        from assistant.policy import AssistantError
        async with get_db_session() as db:
            with pytest.raises(AssistantError) as denied:
                await _check_confirmation_replay(db, ctx, altered)
            assert denied.value.code == "ASSISTANT_CONFIRMATION_INPUT_CHANGED"
            await _check_confirmation_replay(db, ctx, args)
        async with get_db_session() as db:
            cards = (await db.scalars(select(QuestionCheckpoint).where(
                QuestionCheckpoint.session_id == main.id))).all()
            assert len(cards) == 1 and cards[0].status == "answered"
            assert not cards[0].continuation["assistant_send"].get("consumed")
            assert (await db.get(QuestionCheckpoint, args["request_id"])).status == "pending"
            item = await db.scalar(select(AgentInboxItem).where(
                AgentInboxItem.run_id == ctx.run_id, AgentInboxItem.generation == ctx.run_generation))
            item.origin, item.origin_ref = original_origin, original_ref
        accepted, _ = await tool_call(ctx, "requests.revise", args)
        assert accepted.get("state") == "accepted", accepted
        async with get_db_session() as db:
            item = await db.get(AgentInboxItem, accepted["inbox_id"])
            assert item.session_id == session.id and item.attachments == args["attachment_ids"]
    finally:
        await lease.release(session_status="idle")


async def test_current_confirmed_revision_survives_restricted_recall_after_restart(monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from core.config import get_config
    from assistant.confirmations import CONFIRM
    from assistant.projection import project_main_messages
    from session.agent_event_log import load_canonical_model_surface
    from session.session import create_assistant_message
    from tests.unit.test_assistant_sessions_v2 import finish_turn

    owner, _, _, main, _, ctx, lease, args = await setup(shared=True)
    with legacy_send_cards():
        result, _ = await tool_call(ctx, "requests.revise", args)
    await finish_turn(ctx, lease, "等待确认发送。")
    await lease.release(session_status="idle")
    await q.reply(result["suspended"], [[CONFIRM]], user_id=owner)
    from question.continuation import apply_answers, resume_main_through_inbox
    generation = await apply_answers(main.id, owner)
    assert generation is not None
    assert await resume_main_through_inbox(main.id, owner, generation)
    resumed = await reserve_run(main.id, owner)
    try:
        batch = await inbox.claim_inbox_boundary(resumed, step=1, include_next_turn=True)
        message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
            agent="assistant", user_id=owner, run_fence=(main.id, resumed.run_id, resumed.generation))
        ctx = replace(ctx, run_id=resumed.run_id, run_generation=resumed.generation, message_id=message.id)
        config = get_config().model_copy(deep=True)
        config.memory.semantic_recall = True
        config.memory.retrieval_v2 = True
        config.memory.allowed_user_ids = []
        monkeypatch.setattr("core.config.get_config", lambda: config)
        ctx.memory_turn = SimpleNamespace(read_policy="uncertain", explicit_read=False)
        from memory.packet import TurnContext, prepare_context
        async def must_not_classify(*_):
            pytest.fail("A current server card answer must not invoke the semantic router")
        packet = await prepare_context(TurnContext(user_id=owner, workspace_id=ctx.workspace_id,
            session_id=main.id, turn_id=batch.messages[0].id, include_all_projects=True,
            query=""), config.memory, evaluator=must_not_classify)
        assert packet["route"]["reason_code"] == "current_card_continuation"
        assert not packet["route"]["called"] and packet["route"]["read_policy"] == "current_only"
        assert not packet["items"] and not packet["stable_background"]["items"]
        from memory.packet import _packet_state
        async with get_db_session() as db:
            assert not (await _packet_state(db, TurnContext(user_id="another-user",
                workspace_id=ctx.workspace_id, session_id=main.id, turn_id=batch.messages[0].id, query=""),
                continuation=True))["card_continuation"]
            assert not (await _packet_state(db, TurnContext(user_id=owner,
                workspace_id=ctx.workspace_id, session_id=main.id, turn_id="another-message", query=""),
                continuation=True))["card_continuation"]
        surface = await load_canonical_model_surface(main.id, user_id=owner, run_fence=ctx.run_fence)
        projected = await project_main_messages(list(surface.messages), ctx=ctx)
        [block] = [m for m in projected if m.id == "assistant:confirmed-revision"]
        assert args["attachment_ids"][0] in block.parts[0]["text"]
        assert args["request_id"] in block.parts[0]["text"]
        assert "arguments unchanged" in block.parts[0]["text"]
        assert not any(m.id == "assistant:recent-call" for m in projected)
        from assistant.runtime import runtime_view
        from assistant.follow_through import unfinished_revision
        before = await runtime_view(session_id=main.id, user_id=owner,
            run_id=resumed.run_id, generation=resumed.generation)
        assert before["tool_ids"] == frozenset({"requests.revise"})
        # A provider may wrap the entire call in an `arguments` field. This
        # is malformed input, not proof that the user's pending card changed.
        # The public hook runs before tool validation, so exercise it too.
        import json
        from assistant.runtime import authorize_assistant_tool
        from assistant.policy import AssistantError
        for malformed in ({"arguments": json.dumps(args, ensure_ascii=False)},
                          {**args, "request_id": "different-task"},
                          {**args, "version": "b" * 64}):
            with pytest.raises(AssistantError) as denied:
                await authorize_assistant_tool(ctx, "requests.revise", malformed)
            assert denied.value.code == "ASSISTANT_CONFIRMATION_INPUT_CHANGED"
            assert "top-level" in str(denied.value)
            correction = json.loads(str(denied.value)[str(denied.value).index("{"):])
            assert correction == args
        async with get_db_session() as db:
            card = await db.get(QuestionCheckpoint, result["suspended"])
            assert card.status == "answered" and not card.continuation["assistant_send"].get("consumed")
            assert (await db.get(QuestionCheckpoint, args["request_id"])).status == "pending"
        reminder = await unfinished_revision(ctx, [message.id])
        assert "exact saved arguments" in reminder and args["request_id"] in reminder
        accepted, _ = await tool_call(ctx, "requests.revise", correction)
        assert accepted.get("state") == "accepted", accepted
        after = await runtime_view(session_id=main.id, user_id=owner,
            run_id=resumed.run_id, generation=resumed.generation)
        assert after == before  # consuming consent cannot widen the current run
        assert await unfinished_revision(ctx, [message.id]) is None
    finally:
        await resumed.release(session_status="idle")


async def test_read_leaves_card_pending_and_authorized_revision_replaces_it_directly():
    owner, _, _, _, session, ctx, lease, args = await setup(shared=True)
    try:
        result, _ = await tool_call(ctx, "requests.list", {"kind": "question"})
        assert any(item["id"] == args["request_id"] for item in result["other_conversations"])
        inspected, _ = await tool_call(ctx, "requests.get", {"kind": "question", "request_id": args["request_id"]})
        assert inspected["state"] == "pending" and inspected["task_id"] is None
        assert inspected["version"] == args["version"]
        async with get_db_session() as db:
            assert (await db.get(QuestionCheckpoint, args["request_id"])).status == "pending"
            assert await db.scalar(select(AssistantTask.id).where(AssistantTask.execution_session_id == session.id)) is None
        accepted, _ = await tool_call(ctx, "requests.revise", args)
        assert accepted.get("state") == "accepted", accepted
        replay, _ = await tool_call(ctx, "requests.revise", args)
        assert replay == accepted  # A retry must not ask for send consent twice.
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("unavailable", ["expired", "consumed"])
async def test_old_revision_receipt_cannot_start_an_unapproved_replay(unavailable):
    from datetime import timedelta
    from assistant.confirmations import ANSWER_VALID_FOR, CONFIRM
    from question import runtime
    owner, _, _, main, session, ctx, lease, args = await setup(shared=True)
    try:
        with legacy_send_cards():
            result, _ = await tool_call(ctx, "requests.revise", args)
        await q.reply(result["suspended"], [[CONFIRM]], user_id=owner)
        async with get_db_session() as db:
            receipt = await db.get(QuestionCheckpoint, result["suspended"])
            item = await db.scalar(select(AgentInboxItem).where(
                AgentInboxItem.run_id == ctx.run_id, AgentInboxItem.generation == ctx.run_generation))
            item.origin = "system_recovery"
            item.origin_ref = {"entrypoint": "question_answer", "generation": receipt.generation}
            if unavailable == "expired":
                receipt.updated_at = runtime.now() - ANSWER_VALID_FOR - timedelta(seconds=1)
            else:
                receipt.continuation = {**receipt.continuation, "assistant_send": {
                    **receipt.continuation["assistant_send"], "consumed": True}}
        from assistant.policy import AssistantError
        from assistant.revisions import _check_confirmation_replay
        async with get_db_session() as db:
            with pytest.raises(AssistantError) as denied:
                await _check_confirmation_replay(db, ctx, args)
            assert denied.value.code == "ASSISTANT_CONFIRMATION_UNAVAILABLE"
        async with get_db_session() as db:
            assert (await db.get(QuestionCheckpoint, args["request_id"])).status == "pending"
            assert await db.scalar(select(AssistantTask.id).where(
                AssistantTask.execution_session_id == session.id)) is None
            assert len(list(await db.scalars(select(QuestionCheckpoint).where(
                QuestionCheckpoint.session_id == main.id)))) == 1
    finally:
        await lease.release(session_status="idle")


async def test_ordinary_card_read_rejects_another_owners_question():
    _, other, workspace, main, _, ctx, lease, _ = await setup()
    foreign = await conversation(other, workspace, main, "另一人的任务", visibility="workspace")
    request_id, _ = await ask_in(foreign, other)
    try:
        value, _ = await tool_call(ctx, "requests.get", {"kind": "question", "request_id": request_id})
        assert value["error"] == "ASSISTANT_REQUEST_UNAVAILABLE"
    finally:
        await lease.release(session_status="idle")


async def test_revision_racing_a_manual_card_answer_applies_only_one_transition():
    owner, _, _, _, session, ctx, lease, args = await setup()
    try:
        results = await asyncio.gather(tool_call(ctx, "requests.revise", args),
                                     q.reply(args["request_id"], [["可以"]], user_id=owner),
                                     return_exceptions=True)
        async with get_db_session() as db:
            card = await db.get(QuestionCheckpoint, args["request_id"])
            items = list((await db.scalars(select(AgentInboxItem).where(
                AgentInboxItem.session_id == session.id, AgentInboxItem.origin == "assistant_delegation"))).all())
            assert card.status in {"answered", "superseded"}, results
            assert len(items) == int(card.status == "superseded"), results
            assert (card.answers is None) == (card.status == "superseded")
    finally:
        await lease.release(session_status="idle")


async def test_lost_wake_returns_committed_revision_receipt(monkeypatch):
    _, _, _, _, _, ctx, lease, args = await setup()
    def fail(*_):
        raise RuntimeError("wake unavailable")
    monkeypatch.setattr(inbox, "schedule_inbox_wake", fail)
    try:
        result, _ = await tool_call(ctx, "requests.revise", args)
        assert result["state"] == "accepted"
        async with get_db_session() as db:
            assert (await db.get(AgentInboxItem, result["inbox_id"])).state == "accepted"
    finally:
        await lease.release(session_status="idle")


async def test_history_pagination_retains_scope_when_only_page_size_changes():
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "水果店历史", visibility="private")
    for i in range(3):
        await human_turn(session.id, owner, f"要求{i}", f"回复{i}")
    scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id, session_id=session.id, include_tools=False)
    first = await read_history(**scope, limit=2, max_chars=5)
    second = await read_history(**scope, cursor=first["next_cursor"], limit=10, max_chars=5)
    assert first["next_cursor"] and second["items"]
    assert first["items"] != second["items"]
