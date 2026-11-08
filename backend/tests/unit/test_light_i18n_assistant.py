"""The three assistants follow the interface language (docs/LIGHT_MODE_I18N_PLAN.md §2.5).

The personal assistant's prompt (Light and 专业), the helper room a Light job is handed to, and the voice
front desk: each is told the language the App is in, and the voice line's fixed sentences never fall to
Chinese for a Vietnamese call.
"""
from datetime import datetime

from assistant.commands import VIDEO_WORKFLOW_GUIDANCE, workflow_guidance
from assistant.runtime import ASSISTANT_PROMPT
from core import ui_language
from light.prompt import assistant_section, output_language_line
from tests.unit.test_light_i18n import _user
from voice.phrases import english
from voice.prompt import FrontFacts, front_instructions
from voice.tools import state_label


def test_assistant_prompt_puts_the_interface_language_first():
    assert "Use the interface language when the 界面模式 section below names one" in ASSISTANT_PROMPT
    assert "otherwise the user's language (Chinese unless they write in another language)" in ASSISTANT_PROMPT


async def test_pro_section_names_the_language_only_when_the_user_chose_one():
    user = await _user()
    assert await assistant_section(user["id"], user["default_workspace_id"]) == "界面模式：专业。"
    await ui_language.set_ui_language(user["id"], "en-US")
    section = await assistant_section(user["id"], user["default_workspace_id"])
    assert section == "界面模式：专业。 " + output_language_line("en-US") and "英文（English）" in section
    assert await assistant_section(None, None) == "界面模式：专业。"


async def test_light_helper_room_guidance_carries_the_output_language():
    user = await _user()
    zh = await workflow_guidance("menu-maker", user["id"])
    assert zh.startswith(VIDEO_WORKFLOW_GUIDANCE["menu-maker"]) and zh.endswith(output_language_line("zh-CN"))
    await ui_language.set_ui_language(user["id"], "vi-VN")
    vi = await workflow_guidance("footage-edit", user["id"])
    assert "越南语（Tiếng Việt）" in vi and "保留英文专有名词" in vi
    # the main line's video rooms are untouched
    assert await workflow_guidance("video-production", user["id"]) == VIDEO_WORKFLOW_GUIDANCE["video-production"]
    assert await workflow_guidance("video", None) == VIDEO_WORKFLOW_GUIDANCE["video"]


def test_voice_front_desk_starts_in_the_interface_language():
    now = datetime(2026, 11, 8, 10, 0)
    vi = front_instructions(FrontFacts(), "vi", now)
    assert "用户的界面语言是越南语，先用越南语和用户交谈" in vi and "不说中文" in vi
    en = front_instructions(FrontFacts(), "en", now)
    assert "用户的界面语言是英文" in en and "越南语" not in en
    zh = front_instructions(FrontFacts(), "zh", now)
    assert "界面语言是" not in zh


def test_fixed_voice_sentences_are_english_for_a_vietnamese_call():
    assert english("en") and english("vi")
    assert not english("zh") and not english(None) and not english("fr")
    assert state_label("queued", "vi") == "waiting to start" == state_label("queued", "en")
    assert state_label("queued", "zh") != "waiting to start"
