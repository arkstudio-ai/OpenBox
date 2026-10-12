"""Delivery contracts preserve evidence, without samples or literal speech classifiers."""
import json
from datetime import datetime, timezone
import pytest
from voice import phrases
from voice.prompt import FRONT, FrontFacts, front_instructions, voice_turn_block


@pytest.mark.parametrize("status", ["ok", "running", "failed", "timeout", "unavailable"])
def test_result_event_preserves_the_facts_and_does_not_assert_completion(status):
    facts = "预算¥2,900.50；用户选择「方案B」，尚未付款。"
    body = json.loads(phrases.note_body(status, "帮我办这件事", facts, "zh", running=["测试任务"]))
    assert body["status"] == status and body["facts"] == facts
    note = phrases.joined_note([json.dumps(body, ensure_ascii=False)], "zh")
    assert note.startswith(phrases.NOTE_PREFIX["zh"])
    assert json.loads(note.removeprefix(phrases.NOTE_PREFIX["zh"]))["purpose"] == "deliver_results"
    assert "办好了" not in phrases.phrase_text("result_in_text", "zh")


def test_complete_card_identity_and_option_fields_survive_delivery():
    card = {"card": "2", "version": "v1", "options": [{"id": "new", "label": "新项目", "fields": ["project_name"]}]}
    note = json.loads(phrases.card_note("选择放置位置", "", [card], "zh").removeprefix(phrases.NOTE_PREFIX["zh"]))
    assert note["cards"] == [card] and note["operation_state"] == "waiting_for_user"


def test_form_delivery_does_not_hide_later_fields():
    form = {"request_id": "q1", "questions": [{"question": "时长"}, {"question": "字幕"}]}
    note = json.loads(phrases.question_note([form], "zh").removeprefix(phrases.NOTE_PREFIX["zh"]))
    assert note["waiting_for_user"] == [form]


def test_preferences_and_capabilities_do_not_force_sentence_counts_or_an_emotion_ban():
    assert "一两句" not in FRONT and "不说紧张" not in FRONT and "就不出声" not in FRONT
    assert "语气、语速" in FRONT and "decision_recorded" in FRONT
    text = front_instructions(FrontFacts(name="小七", address="阿云", detail="detailed"), "zh", datetime.now(timezone.utc))
    assert "小七" in text and "阿云" in text and "按问题需要展开" in text
    assert "two or three" not in voice_turn_block({})
    assert "four or five" not in voice_turn_block({"detail": "detailed"})


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_terminal_notices_are_status_facts_and_backgrounds_are_not_user_requests(lang):
    assert phrases.phrase_text("credits_exhausted", lang) in phrases.phrase_instructions("credits_exhausted", lang)
    assert phrases.delivery_instructions("anything", lang) == phrases.notice_instructions(lang)
    assert phrases.greeting_instructions(lang, False) != phrases.greeting_instructions(lang, True)
