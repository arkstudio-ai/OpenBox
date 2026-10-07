"""Fixed phrases and the response-level instructions the front desk follows word for word."""
import pytest

from voice import phrases

TEXTS = {
    "greeting": ("嗨，我在，你说。", "Hi, I'm here. Go ahead."),
    "still_working": ("还在办，好了我马上告诉你。", "Still on it. I'll tell you as soon as it's done."),
    "result_in_text": ("办好了，结果我写在对话里了。", "Done. I've put the result in the conversation."),
    "limit_reached": ("这通电话到时间了，我们文字里继续。", "This call has reached its time limit. Let's continue in text."),
}


@pytest.mark.parametrize("key", sorted(TEXTS))
def test_each_phrase_is_said_alone_without_tools(key):
    zh, en = TEXTS[key]
    assert phrases.phrase_instructions(key, "zh") == "只说这一句，不要调用任何工具，不要加别的话：" + zh
    assert phrases.phrase_instructions(key, "en").endswith(": " + en)
    assert "no tools" in phrases.phrase_instructions(key, "en")
    assert phrases.phrase_instructions(key, "fr") == phrases.phrase_instructions(key, "zh")


def test_a_result_is_read_word_for_word_after_what_i_found():
    speech = "「贪吃蛇」的收尾自检昨晚做完了，一切正常；「配色」还在等你选一个方案。"
    instructions = phrases.delivery_instructions(speech, "zh")
    assert instructions == ("个人助理的结果回来了。请逐字朗读下面这段话，一个字都不要增减或改写，不要调用工具："
                            "我这边查到了，" + speech)
    assert instructions.split("：", 1)[1].startswith("我这边查到了") and instructions.endswith(speech)
    english = phrases.delivery_instructions("The tests pass.", "en")
    assert english.endswith("word for word, without adding, removing or rewording anything, and call no tools: "
                            "Here's what I found. The tests pass.")


def test_a_notice_is_verbatim_but_claims_no_result():
    notice = phrases.notice_instructions(phrases.speech_text("timeout", "zh"), "zh")
    assert notice.endswith("这件事还在办，办好了我在对话里告诉你。") and "查到了" not in notice


def test_unknown_phrases_are_refused():
    with pytest.raises(KeyError):
        phrases.phrase_instructions("goodbye", "zh")


def test_front_prompt_adds_only_known_facts_to_the_tested_text():
    from datetime import datetime
    from voice.prompt import FRONT, front_instructions
    now = datetime(2026, 10, 7, 17, 30)
    bare = front_instructions("", "", "zh", now)
    assert bare == FRONT + "今天是 2026年10月7日 星期三 17:30。"  # nothing invented when nothing is known
    full = front_instructions("喜欢简短的回答", "贪吃蛇的收尾自检做完了", "en", now)
    assert full.endswith("先用英文和用户交谈。关于用户：喜欢简短的回答。最近聊过：贪吃蛇的收尾自检做完了。")
    assert "assistant_ask" in FRONT and "直说不知道" in FRONT
