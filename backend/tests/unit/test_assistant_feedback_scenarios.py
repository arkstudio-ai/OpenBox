"""Reproduce production's malformed placement and interleaved task preparation."""
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from assistant.requests import question_version
from db.base import get_db_session
from db.models.assistant import AssistantTask
from question import question as q
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_pending import pause, saved_request
from tests.unit.test_assistant_placement import counts, prepare, quiet  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import card, main_turn, tool_call
from tool.assistant_tools import SubmitArgs


PROPOSAL = {"project_name": "短视频运营", "question": "这项视频任务放在短视频运营项目里，可以吗？"}


@pytest.mark.parametrize("encoded", [json.dumps(PROPOSAL, ensure_ascii=False), str(PROPOSAL)])
async def test_encoded_placement_still_requires_choice_and_replays_same_request(encoded):
    owner, _, _, ctx, lease, args = await prepare()
    try:
        before = await counts(owner)
        asked, _ = await tool_call(ctx, "tasks.submit", {**args, "placement": encoded})
        row = await card(asked["suspended"])
        assert row.questions[0]["question"] == PROPOSAL["question"]
        assert await counts(owner) == before
        await q.reply(row.id, [["取消"]], owner)
        # Both wire representations have one canonical identity and consent path.
        assert (await tool_call(ctx, "tasks.submit", {**args, "placement": PROPOSAL}))[0] == {"state": "cancelled"}
        assert await counts(owner) == before
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("value", [
    "[]", "null", "42", "dict(project_name='x', question='q')",
    "{'project_name':'x', 'question':'q', 'destination_confirmed':True}",
    "{'project_name':'x'}", "{'project_name':[], 'question':'q'}", "x" * 8193,
])
def test_encoded_placement_never_executes_code_or_bypasses_schema(value):
    with pytest.raises(ValidationError):
        SubmitArgs(title="样片", instructions="只准备", source_message_ids=["human"], placement=value)


async def test_three_preparations_survive_topic_changes_and_only_chosen_task_starts():
    owner, workspace, main, original, first_id, _ = await saved_request()
    requests = [(first_id, original)]
    for title in ("素材识别原因排查", "模型上传路径查看"):
        ctx, lease, human = await main_turn(owner, workspace, main, f"另准备一项：{title}")
        args = {**original, "title": title, "instructions": title, "source_message_ids": [human]}
        result, _ = await tool_call(ctx, "tasks.submit", args)
        requests.append((result["suspended"], args))
        await pause(ctx, lease)
    ctx, lease, human = await main_turn(owner, workspace, main, "第二项素材识别原因排查放到默认空间，其他两项先保留")
    try:
        listed, _ = await tool_call(ctx, "cards.list", {})
        assert {item["request_id"] for item in listed["items"]} == {rid for rid, _ in requests}
        assert all(not item["execution_confirmed"] for item in listed["items"])
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(AssistantTask)) == 0
        target, args = requests[1]
        row = await card(target)
        answer, _ = await tool_call(ctx, "cards.answer", {
            "request_id": target, "version": question_version(row),
            "answers": [[row.questions[0]["options"][0]["label"]]], "source_message_ids": [human],
        })
        assert answer["original_call"]["arguments"] == args
        receipt, _ = await tool_call(ctx, "tasks.submit", answer["original_call"]["arguments"])
        assert receipt["state"] == "accepted"
        assert (await tool_call(ctx, "tasks.submit", args))[0] == receipt
        async with get_db_session() as db:
            tasks = list((await db.scalars(select(AssistantTask))).all())
            assert len(tasks) == 1 and tasks[0].title == args["title"]
        for rid, _ in (requests[0], requests[2]):
            assert (await card(rid)).status == "pending"
    finally:
        await lease.release(session_status="idle")
