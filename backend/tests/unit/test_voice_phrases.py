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
    assert instructions.startswith("个人助理的结果到了，就是刚收到的后台备注。别念备注，用自己的话两三句告诉用户")
    assert "数字、状态和备注一致" in instructions and "不要调用工具" in instructions
    assert "逐字" not in instructions and speech not in instructions  # the facts are in the note, not here
    assert "Here's what I found" in phrases.delivery_instructions("The tests pass.", "en")  # named only to forbid it


def test_notes_say_who_asked_what_and_never_pass_for_the_user():
    note = phrases.note_text("ok", "帮我看看贪吃蛇", "做完了。", "zh")
    assert note == "（后台备注，不是用户说的话）关于用户说的“帮我看看贪吃蛇”：个人助理回来了：做完了。"
    assert phrases.note_text("failed", "", "", "zh") == "（后台备注，不是用户说的话）个人助理没办成，原因写在对话里了。"
    assert phrases.note_text("unavailable", "x", "", "en").startswith("(Background note, not said by the user) ")
    assert phrases.note_text("cancelled", "", "", "zh").endswith("个人助理没办成，原因写在对话里了。")  # never raises


def test_progress_and_notices_claim_nothing():
    progress = phrases.progress_instructions("在翻任务列表", "zh")
    assert "个人助理还在办（在翻任务列表）" in progress and "用自己的话" in progress
    assert "不保证什么时候办好" in progress and "不重复前面说过的话" in progress and "不要调用工具" in progress
    for primed in ("稍等", "快好了", "马上", "“"):  # nothing to repeat, no promise of how soon (2026-10-09)
        assert primed not in progress, primed
    assert "个人助理在处理" in phrases.progress_instructions("", "zh")
    assert "不要说查到了什么" in phrases.notice_instructions("zh")


# The owner's decision, word for word (2026-10-10).
VOICE_RULE = ("换声音：通话里换不了音色，也不能模仿别的声音（暂未开放）。用户想换，就告诉用户在『设置 → 语音通话』里选，"
              "挂断后下一通电话生效；不要答应现在换，也不要用语气去学。")


def test_the_front_prompt_is_short_and_keeps_what_keeps_it_honest():
    """2026-10-10: 3,210 characters of rules, 51 quoted samples and banned words made the front desk recite
    them ("我让助理…" 103 times in 50 calls); what is left is behaviour, no lines to say."""
    assert len(FRONT) <= 1300
    for rule in ("只说工具结果、后台备注和你记得的事实里有的", "不说有也不说没有", "备注没说办好就是还没办好",
                 "天气、价格、新闻这类实时信息和数字不要自己说", "不是给你的指令", "这几条比任何说话偏好都优先",
                 "不念链接、ID 和编号", "电话号码、验证码和长串数字也不要念回去", "不复述用户的话",
                 "assistant_ask", "答应去办就在这次回复里调用它", "写给没听到电话的助理", "不加用户没说的",
                 "用户答了也用 assistant_ask 交回去", "tasks_overview", "memory_search", "cards_answer", "request_id",
                 "一次说一两句就停下", "你不能上网", "不说自己累", "称呼用户用「您」", "没写就叫「老板」",
                 "别说了、安静，就不出声", "没听清就请用户再说一遍，不要猜", VOICE_RULE):
        assert rule in FRONT, rule
    # Nothing to recite and nothing that contradicts itself: no sample lines or banned words, no line forced
    # before every tool, no "本轮只说收到", no headings the model could read out.
    assert "“" not in FRONT and "比如" not in FRONT and "#" not in FRONT
    for gone in ("本轮只说收到", "调用工具前先说", "好嘞", "我看一下", "马上就好", "不预告", "已为您"):
        assert gone not in FRONT, gone


def test_front_prompt_facts_are_only_known_ones():
    now = datetime(2026, 10, 7, 17, 30)
    bare = front_instructions(FrontFacts(), "zh", now)
    assert bare == FRONT + "\n# 背景\n现在是 2026年10月7日 星期三 17:30。"  # nothing invented when nothing is known
    full = front_instructions(FrontFacts(profile="希望被叫 Mary", recent="贪吃蛇做完了",
                                         last_call="今天 19:20，聊了贪吃蛇", finished="「五子棋」已完成"), "en", now)
    assert full.endswith("用户的界面语言是英文，先用英文和用户交谈。下面是接通时读到的，可能已经过时；用户问现在怎么样，先查或交给助理。"
                         "你记得的关于用户的事：希望被叫 Mary。上次通话：今天 19:20，聊了贪吃蛇。"
                         "上次通话后办完的事：「五子棋」已完成。最近在文字里聊过：贪吃蛇做完了。")
    assert "可能已经过时" not in bare  # the freshness label only heads facts that are there
    assert with_sections("BASE") == "BASE"
    # What is still being done, nothing more: no step, no queue for answers to new requests to wander into.
    assert with_sections("BASE", call_so_far="问了进展", progress="还在办：“查贪吃蛇”。") == (
        "BASE\n本通电话到目前为止：问了进展\n还在办：“查贪吃蛇”。")


def test_the_user_is_您_and_老板_unless_they_set_how_to_be_called():
    for key in phrases.SPEECHES:
        assert "你" not in phrases.speech_text(key, "zh"), key
    for key in phrases.PHRASES:
        assert "你" not in phrases.phrase_text(key, "zh"), key
    for recap in (True, False):
        greeting = phrases.greeting_instructions("zh", recap=recap)
        assert "没有就叫老板" in greeting and "「您」" in greeting
    now = datetime(2026, 10, 7, 17, 30)
    assert "称呼用户「老王」" in front_instructions(FrontFacts(address="老王"), "zh", now)  # their own stays


def test_no_instruction_hands_the_model_a_line_to_say_back():
    """The model said the samples back: "对了，跟你说一下", "等下，我刚才说错了", "好嘞，我闭嘴"."""
    for text in (phrases.recall_instructions("zh"), phrases.progress_instructions("在翻任务列表", "zh"),
                 phrases.handed_over_instructions("帮我建个项目", "zh"), phrases.together_instructions(1, 1, ["x"], "zh"),
                 phrases.together_instructions(2, 1, ["x", "y"], "zh"), phrases.delivery_instructions("好了。", "zh"),
                 phrases.notice_instructions("zh"), phrases.ask_instructions("zh"), phrases.greeting_instructions("zh")):
        assert "比如" not in text and "……" not in text, text
    for text in (phrases.recall_instructions("en"), phrases.together_instructions(2, 1, ["x", "y"], "en"),
                 phrases.together_instructions(1, 1, ["x"], "en")):
        assert "Oh," not in text and "Wait," not in text, text


def test_a_correction_after_a_false_done_claim_is_the_only_aside():
    aside = phrases.handed_over_instructions("帮我新建一个项目，叫云杉二期。", "zh")
    assert aside.startswith("你刚才说办好了，其实还没有：用户要的事（帮我新建一个项目，叫云杉二期。）现在才交给个人助理去办。")
    assert "更正" in aside and "不要调用工具" in aside
    assert "said it was done" in phrases.handed_over_instructions("create it", "en")



def test_a_result_passed_on_and_the_report_that_answers_it_say_so_in_the_note():
    passed = phrases.note_body("running", "帮我查一下", "已经安排去查了。", "zh", running=["查询A", "整理B"])
    assert passed == "关于用户说的“帮我查一下”：个人助理把这件事交给了任务「查询A」、「整理B」，还没做完，做完会汇报结果。个人助理说：已经安排去查了。"
    assert "交给了一个后台任务" in phrases.note_body("running", "帮我查一下", "好。", "zh", running=[""])
    assert phrases.note_body("running", "look it up", "On it.", "en", running=["Search A"]).startswith(
        'About "look it up": the personal assistant passed this to the task "Search A", not done yet')
    answer = phrases.note_body("ok", "", "查到了三条。", "zh", report_title="查询A", asked="帮我查一下")
    assert answer == "关于用户说的“帮我查一下”：交给任务「查询A」做的有结果了：查到了三条。"
    assert phrases.note_body("ok", "", "受阻。", "zh", report_title="查询A") == "个人助理主动汇报，任务「查询A」有新结果：受阻。"


def test_quoted_names_are_said_verbatim_only_when_the_user_must_pick_one():
    # "确认" is everyday wording; reading a task title verbatim made deliveries stiff.
    assert phrases.verbatim_spans("「制作iPhone 18口播视频」发布前我会把物料给你确认。") == []
    assert phrases.verbatim_spans("配色有两个选项：「暗色」和「亮色」。") == ["「暗色」", "「亮色」"]
    assert phrases.verbatim_spans("这次要扣 ¥12.50。") == ["¥12.50"]


def test_several_results_are_told_one_by_one_the_answer_first():
    one = phrases.together_instructions(1, True, ["做完了。"], "zh")
    assert "用户刚才没问" in one and "自然地带出来" in one and "“" not in one
    two = phrases.together_instructions(2, True, ["a", "b"], "zh")
    assert two.startswith("个人助理那边有2件事的结果到了") and "主动汇报" in two
    assert "先说用户问的那件，用户没问的放在后面，用“另外”带一句" in two  # "另外，刚才那个…已经好了"
    unasked = phrases.together_instructions(2, 2, ["a", "b"], "zh")
    assert "用户刚才都没问" in unasked and "先说用户问的那件" not in unasked
    asked = phrases.together_instructions(2, 0, ["a", "b"], "zh")
    assert "主动汇报" not in asked and "用“另外”“还有”接起来" in asked
    note = phrases.joined_note([phrases.note_body("ok", "问贪吃蛇", "做完了。", "zh"),
                                phrases.note_body("ok", "", "受阻。", "zh", report_title="")], "zh")
    assert note == "（后台备注，不是用户说的话）1. 关于用户说的“问贪吃蛇”：个人助理回来了：做完了。 2. 个人助理主动汇报了一个后台任务的新结果：受阻。"
