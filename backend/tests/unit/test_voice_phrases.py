"""What the front desk is asked to say and the session prompt it works from."""
from datetime import datetime

import pytest

from voice import phrases
from voice.prompt import FRONT, FrontFacts, front_instructions, with_sections

TEXTS = {
    "result_in_text": ("办好了，结果我写在对话里了。", "Done. I've put the result in the conversation."),
    "limit_reached": ("这通电话到时间了，我们文字里继续。", "This call has reached its time limit. Let's continue in text."),
}


@pytest.mark.parametrize("key", sorted(TEXTS))
def test_the_remaining_fixed_notices_are_said_alone_without_tools(key):
    zh, en = TEXTS[key]
    assert phrases.phrase_instructions(key, "zh") == "只说这一句，不要调用任何工具，不要加别的话：" + zh
    assert phrases.phrase_instructions(key, "en").endswith(": " + en)
    assert phrases.phrase_instructions(key, "fr") == phrases.phrase_instructions(key, "zh")


def test_no_fixed_greeting_or_stock_phrases_remain():
    for key in ("greeting", "still_working"):
        with pytest.raises(KeyError):
            phrases.phrase_instructions(key, "zh")
    greeting = phrases.greeting_instructions("zh")
    assert "时间" in greeting and "称呼" in greeting and "上次通话" in greeting and "新办完的事" in greeting
    assert "“" not in greeting and "只说这一句" not in greeting  # a goal, never a sentence to say
    assert "上次通话里没有结果的事不要说办完了" in greeting
    assert "Greet the user" in phrases.greeting_instructions("en")


def test_a_result_is_told_in_own_words_with_its_facts_unchanged():
    speech = "「贪吃蛇」的收尾自检昨晚做完了，一切正常。"
    instructions = phrases.delivery_instructions(speech, "zh")
    assert instructions.startswith("个人助理的结果到了，就是刚收到的后台备注。用你自己的话")
    assert "三句以内" in instructions and "名字、数字、状态、选项必须和备注一致" in instructions
    assert "逐字" not in instructions and speech not in instructions  # the facts are in the note, not here
    assert "Here's what I found" in phrases.delivery_instructions("The tests pass.", "en")  # named only to forbid it


def test_notes_say_who_asked_what_and_never_pass_for_the_user():
    note = phrases.note_text("ok", "帮我看看贪吃蛇", "做完了。", "zh")
    assert note == "（后台备注，不是用户说的话）关于用户说的“帮我看看贪吃蛇”：个人助理回来了：做完了。"
    assert phrases.note_text("failed", "", "", "zh") == "（后台备注，不是用户说的话）个人助理没办成，原因写在对话里了。"
    assert phrases.note_text("unavailable", "x", "", "en").startswith("(Background note, not said by the user) ")
    assert phrases.note_text("cancelled", "", "", "zh").endswith("个人助理没办成，原因写在对话里了。")  # never raises


def test_progress_and_notices_claim_nothing():
    assert phrases.progress_instructions("在翻你的任务列表", "zh") == (
        "用一句平实的话说说现在在干什么（在翻你的任务列表），不加情绪和感受，不要重复之前说过的话，不要调用工具。")
    assert "个人助理在处理" in phrases.progress_instructions("", "zh")
    assert "不要说查到了什么" in phrases.notice_instructions("zh")


def test_front_prompt_rules_and_only_known_facts():
    now = datetime(2026, 10, 7, 17, 30)
    bare = front_instructions(FrontFacts(), "zh", now)
    assert bare == FRONT + "现在是 2026年10月7日 星期三 17:30。"  # nothing invented when nothing is known
    for rule in ("tasks_overview", "memory_search", "schedules_list", "projects_list", "credits", "assistant_ask",
                 "整句原样交过去", "只有工具结果和“后台备注”里有的事实才能说", "没查过不要说“查到了”",
                 "先等一等，或者追问一句", "只是让你停下", "不说自己累了", "直说这个你查不到", "不念链接、ID、编号"):
        assert rule in FRONT, rule
    assert "我这边查到了" in FRONT and "不用“我这边查到了”" in FRONT  # named only to forbid it
    full = front_instructions(FrontFacts(profile="希望被叫 Mary", recent="贪吃蛇做完了",
                                         last_call="今天 19:20，聊了贪吃蛇", finished="「五子棋」已完成"), "en", now)
    assert full.endswith("用户的界面语言是英文，先用英文和用户交谈。关于用户：希望被叫 Mary。上次通话：今天 19:20，聊了贪吃蛇。"
                         "上次通话后办完的事：「五子棋」已完成。最近在文字里聊过：贪吃蛇做完了。")
    assert with_sections("BASE") == "BASE"
    assert with_sections("BASE", call_so_far="问了进展", progress="在翻任务") == (
        "BASE\n本通电话到目前为止：问了进展\n当前后台进度：在翻任务")
