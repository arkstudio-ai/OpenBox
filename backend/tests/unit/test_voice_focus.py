"""The task a call is about: greeting, instructions and the task_blocker quick read (voice/focus.py)."""
import json

from assistant.commands import accept_task_command
from assistant.control import accept_control_command
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_commands import setup_task
from voice import phrases, tools
from voice.focus import recent_focus, state_phrase
from voice.prompt import with_sections
from voice.tools import CallScope


def test_state_phrases_say_who_does_what_and_never_a_field_name():
    assert "核对过了，继续" in state_phrase("canceled", "effect_unknown")
    assert state_phrase("running", "waiting_input", blocked_by={"question": "用哪张图？"}).endswith("用哪张图？")
    assert state_phrase("paused", "paused") == "暂停中，说继续就接着做"
    assert state_phrase("running", "queued").startswith("排队中")
    assert state_phrase("running", "running", "en") == "in progress; nothing is stuck"
    for text in (state_phrase(d, o) for d in ("running", "paused", "canceled") for o in
                 ("queued", "running", "effect_unknown", "error", "completed", "resume_blocked", "waiting_input")):
        assert "_" not in text and "state" not in text


def test_the_greeting_and_the_instructions_name_the_focused_task():
    focus = {"task_id": "t1", "title": "艾薇兰小红瓶调研", "state": "正在做，没有卡住"}
    greeting = phrases.greeting_instructions("zh", recap=True, focus=focus)
    assert "「艾薇兰小红瓶调研」，正在做，没有卡住" in greeting and "不展开" in greeting
    assert "艾薇兰" not in phrases.greeting_instructions("zh", recap=False, focus=focus)  # recap off: no task talk
    assert "艾薇兰" not in phrases.greeting_instructions("zh", recap=True, first_call=True, focus=focus)
    text = with_sections("BASE", focus=focus)
    assert text.startswith("BASE") and "当前在跟进：「艾薇兰小红瓶调研」，正在做，没有卡住" in text and "先问清是哪一件" in text
    assert with_sections("BASE", focus=None) == "BASE"


async def test_task_blocker_answers_in_a_second_with_a_sentence_to_say():
    owner, _, workspace, main, kwargs = await setup_task()
    created = await accept_task_command(**kwargs, title="艾薇兰小红瓶调研")
    scope = CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id, call_id="call-f")
    value = await tools.run("task_blocker", scope, json.dumps({"title": "艾薇兰"}, ensure_ascii=False))
    assert value["status"] == "ok" and value["task_id"] == created["task_id"] and value["say"].startswith("「艾薇兰小红瓶调研」排队中")
    await accept_control_command(user_id=owner, workspace_id=workspace, main_id=main.id, task_id=created["task_id"],
        idempotency_key="stop-1", action="cancel", expected_revision=created["task_revision"], expected_run=None)
    value = await tools.run("task_blocker", scope, "{}")  # no name: the task touched last
    assert value["task_id"] == created["task_id"] and "已停止" in value["say"] and value["waiting_on_card"] is False
    assert (await tools.run("task_blocker", scope, json.dumps({"title": "不存在的事"}, ensure_ascii=False)))["status"] == "not_found"
    stranger, _, other = await accounts()
    assert (await tools.run("task_blocker", CallScope(user_id=stranger, workspace_id=workspace,
                                                      main_session_id=main.id), "{}"))["status"] == "not_found"
    # The next call opens on it.
    focus = await recent_focus(owner, workspace, main.id)
    assert focus["task_id"] == created["task_id"] and focus["title"] == "艾薇兰小红瓶调研" and "已停止" in focus["state"]
