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


def test_the_instructions_name_the_focused_task_but_the_greeting_never_reads_it_back():
    # 2026-11-03 feedback: every call opened with the name and the last task's title; the task now
    # stays in the focus section (answered when asked) and on the conversation card.
    focus = {"task_id": "t1", "title": "艾薇兰小红瓶调研", "state": "正在做，没有卡住"}
    for lang in ("zh", "en"):
        for recap in (True, False):
            greeting = phrases.greeting_instructions(lang, recap=recap, focus=focus)
            assert "艾薇兰" not in greeting and greeting == phrases.greeting_instructions(lang, recap=recap)
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


def test_a_call_minutes_after_hanging_up_is_told_to_skip_the_opening():
    from datetime import datetime
    from voice.prompt import FrontFacts, front_instructions
    again = front_instructions(FrontFacts(quick_return=True), "zh", datetime(2026, 11, 3, 10, 56))
    assert "本通没有开场白" in again and "不叫称呼" in again
    assert "本通没有开场白" not in front_instructions(FrontFacts(), "zh", datetime(2026, 11, 3, 10, 56))
    # A first contact still introduces itself even when a failed handshake ended seconds ago.
    assert "本通没有开场白" not in front_instructions(FrontFacts(quick_return=True, first_call=True), "zh",
                                                   datetime(2026, 11, 3, 10, 56))
