"""A screen click removes the pending card, not its committed answer receipt."""
from dataclasses import replace
import json

import pytest

from assistant.shared_context import recent_decisions
from db.base import get_db_session
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from memory.packet import TurnContext
from question import question as questions
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_requests_v2 import ask_in
from tests.unit.test_assistant_sessions_v2 import assistant, conversation, quiet_runtime  # noqa: F401
from voice import cards, tools


async def setup():
    owner, other, workspace, main = await assistant()
    task = await conversation(owner, workspace, main, "花店短视频", visibility="private")
    identity, _ = await ask_in(task, owner)
    desk = cards.CardDesk(lambda: (1, "我已经选好了"))
    scope = tools.CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id,
        call_id="receipt-test", desk=desk)
    return owner, other, workspace, task, identity, scope


async def test_manual_answer_remains_readable_after_pending_snapshot_reconciles():
    owner, _, _, _, identity, scope = await setup()
    pending = await cards.pending(scope, {"request_id": identity})
    assert pending["task_questions"][0]["request_id"] == identity
    await questions.reply(identity, [["深色"]], user_id=owner)
    result = await cards.pending(scope, {"request_id": identity})
    assert result["status"] == "ok" and result["waiting_count"] == 0
    assert result["task_questions"] == [] and identity not in scope.desk.questions
    receipt, = result["handled_decisions"]
    assert receipt["request_id"] == identity and receipt["answers"] == [["深色"]]
    assert receipt["execution_confirmed"] is False
    async with get_db_session() as db:
        assert (await db.get(QuestionCheckpoint, identity)).answers == [["深色"]]


async def test_target_filter_precedes_recent_limit():
    owner, _, workspace, task, identity, scope = await setup()
    await questions.reply(identity, [["浅色"]], user_id=owner)
    other_id, _ = await ask_in(await conversation(owner, workspace, task, "另一件事"), owner)
    await questions.reply(other_id, [["深色"]], user_id=owner)
    async with get_db_session() as db:
        for filters in ({"request_id": identity}, {"query": "花店"}):
            found = await recent_decisions(db, owner, workspace, limit=1, **filters)
            assert [r["request_id"] for r in found] == [identity]
    value = await cards.pending(scope, {"query": "花店"})
    assert [r["request_id"] for r in value["handled_decisions"]] == [identity]


async def test_receipt_enforces_owner_workspace_and_deleted_session():
    owner, other, workspace, task, identity, _ = await setup()
    await questions.reply(identity, [["深色"]], user_id=owner)
    async with get_db_session() as db:
        assert await recent_decisions(db, other, workspace, request_id=identity) == []
        assert await recent_decisions(db, owner, "other-workspace", request_id=identity) == []
        (await db.get(Session, task.id)).is_deleted = True
        await db.flush()
        assert await recent_decisions(db, owner, workspace, request_id=identity) == []


async def test_noncontiguous_title_returns_scoped_identities_not_false_empty_or_playback_binding():
    owner, _, _, _, identity, scope = await setup()
    await questions.reply(identity, [["深色"]], user_id=owner)
    value = await cards.pending(scope, {"query": "花店视频"})
    assert value["status"] == "ok" and value["query_matched"] is False
    candidate, = value["candidates"]
    assert candidate["request_id"] == identity and candidate["conversation"] == "花店短视频"
    assert candidate["state"] == "answered" and candidate["answered_at"]
    assert scope.desk.open() == [] and scope.desk.questions == {}
    assert value["handled_decisions"][0]["answers"] == [["深色"]]
    actual = await cards.pending(scope, {"request_id": value["candidates"][0]["request_id"]})
    assert actual["handled_decisions"][0]["answers"] == [["深色"]]


async def test_old_card_read_also_exposes_newer_decisions_from_the_same_task_only():
    from question.continuation import apply_answers
    owner, _, workspace, task, identity, scope = await setup()
    await questions.reply(identity, [["深色"]], user_id=owner)
    assert await apply_answers(task.id, owner) is not None
    newer, _ = await ask_in(task, owner)
    await questions.reply(newer, [["浅色"]], user_id=owner)
    unrelated, _ = await ask_in(await conversation(owner, workspace, task, "别的任务"), owner)
    await questions.reply(unrelated, [["浅色"]], user_id=owner)
    result = await cards.pending(scope, {"request_id": identity})
    later, = result["later_decisions_in_same_conversation"]
    assert later["request_id"] == newer and later["answers"] == [["浅色"]]
    assert result["handled_decisions"][0]["answers"] == [["深色"]]
    assert later["execution_confirmed"] is False


@pytest.mark.parametrize("policy,visible", [("allowed", True), ("no_history", True),
                                            ("current_only", False), ("uncertain", False)])
async def test_task_receipt_obeys_current_turn_read_policy(policy, visible):
    owner, _, workspace, _, identity, scope = await setup()
    await questions.reply(identity, [["深色"]], user_id=owner)
    scope = replace(scope, memory_turn=TurnContext(user_id=owner, workspace_id=workspace,
        query="我的选择呢", read_policy=policy))
    result = await tools.run("cards_pending", scope, json.dumps({"request_id": identity}))
    if visible:
        assert result["handled_decisions"][0]["answers"] == [["深色"]]
    else:
        assert result["status"] == ("unavailable" if policy == "uncertain" else "restricted")
        assert result["items"] == [] and result["checked"] is False
        assert "不能要求重复作答" in result["note"]
