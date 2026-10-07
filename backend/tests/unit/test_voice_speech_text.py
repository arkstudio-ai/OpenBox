"""What the front desk reads aloud: no link targets, marks or identifiers, and a bounded length."""
import pytest

from voice.speech_text import LIMIT, clean


def test_links_keep_their_labels_and_bare_urls_disappear():
    assert clean("[打开「收尾自检」](/app/s/ses_01J9ABCDEFGHJKMNPQRSTVWXYZ)，然后选配色") == "打开「收尾自检」，然后选配色。"
    assert clean("![封面](https://cdn.example.com/a.png) 已生成") == "封面已生成。"
    assert "http" not in clean("详见 https://example.com/a?b=1 和 www.example.cn/x")
    assert clean("看这里 <https://example.com>") == "看这里。"


def test_lists_headings_emphasis_quotes_and_tables_become_plain_sentences():
    text = "## 进展\n- **贪吃蛇**：收尾自检做完了\n* 配色还在等你选\n1. 暖橙\n2) 雾蓝\n> 备注\n| 名称 | 状态 |\n|---|---|"
    assert clean(text) == "进展。贪吃蛇：收尾自检做完了。配色还在等你选。暖橙。雾蓝。备注。名称，状态。"
    assert clean("Done! See *this*, _that_ and ~~old~~ snake_case_name.", lang="en") == \
        "Done! See this, that and old snake_case_name."


def test_code_fences_and_inline_code_marks_are_removed():
    assert clean("运行下面的命令：\n```bash\nnpm test\n```\n然后用 `pytest` 再跑一遍") == "运行下面的命令：npm test.然后用pytest再跑一遍。"


@pytest.mark.parametrize("identifier", [
    "01J9ABCDEFGHJKMNPQRSTVWXYZ", "inbox_01J9ABCDEFGHJKMNPQRSTVWXYZ",
    "9f86d081884c7d659a2feaa0c55ad015", "123e4567-e89b-12d3-a456-426614174000",
])
def test_identifiers_nobody_can_say_are_removed(identifier):
    assert clean(f"任务（ID: {identifier}）已创建，编号 {identifier}") == "任务已创建，编号。"


def test_words_and_numbers_that_only_look_like_identifiers_stay():
    assert clean("deadbeefdeadbeefdead") == "deadbeefdeadbeefdead."
    assert clean("一共 12 个任务，3.5 元") == "一共12个任务，3.5元。"


def test_long_replies_stop_at_a_sentence_end_and_point_to_the_conversation():
    text = "我" * 150 + "。" + "你" * 200 + "。"
    spoken = clean(text)
    assert spoken == "我" * 150 + "。详细的我写在对话里了。"
    english = clean("This is one sentence. " * 30, lang="en")
    assert english.endswith("sentence. The details are in the conversation.") and len(english) < LIMIT + 40
    unbroken = clean("长" * 400)
    assert unbroken == "长" * LIMIT + "……详细的我写在对话里了。"


@pytest.mark.parametrize("empty", [None, "", "   \n ", "https://example.com", "```\n```"])
def test_empty_or_unspeakable_input_gives_nothing(empty):
    assert clean(empty) == ""
