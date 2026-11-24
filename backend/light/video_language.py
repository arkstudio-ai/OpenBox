"""Choose generated speech from the brief, independent of the interface locale."""

import re
import unicodedata
from typing import Literal

from core.ui_language import speech_language

Language = Literal["zh", "en", "vi"]
NAMES = {"zh": "普通话", "en": "English", "vi": "Tiếng Việt (Vietnamese)"}
LOCALES = {"zh": "zh-CN", "en": "en-US", "vi": "vi-VN"}
END_CARD = {"zh": "片尾", "en": "End card", "vi": "Thẻ kết"}
MEDIA_TAGS = {"zh": "zho", "en": "eng", "vi": "vie"}

_OUTPUT_LANGUAGE = re.compile(
    r"(?:用|使用|输出|说|配音为|字幕用)\s*(越南语|英语|英文|普通话|中文)"
    r"|(越南语|英语|英文|普通话|中文)(?:的)?(?:对白|字幕|成片|视频|旁白)"
    r"|(?:\bin\s+|\bbằng\s+|\bdùng\s+)(vietnamese|tiếng việt|tieng viet|english|tiếng anh|tieng anh|mandarin|chinese|tiếng trung|tieng trung)\b",
    re.I,
)
_DECLARED = {
    "越南语": "vi", "vietnamese": "vi", "tiếng việt": "vi", "tieng viet": "vi",
    "英语": "en", "英文": "en", "english": "en", "tiếng anh": "en", "tieng anh": "en",
    "普通话": "zh", "中文": "zh", "mandarin": "zh", "chinese": "zh", "tiếng trung": "zh", "tieng trung": "zh",
}
_VIETNAMESE = re.compile(r"[Ạ-ỹĐđĂăƠơƯư]|\b(?:tiếng việt|tieng viet)\b", re.I)
_VI_PLAIN = re.compile(r"\b(?:tao video|lam video|quang cao|phu de|loi thoai|loi dan|cua hang|mon an|cho toi)\b", re.I)
_HAN = re.compile(r"[\u3400-\u9fff]")
_ENGLISH = re.compile(r"\b(?:the|and|you|with|this|make|for|please|my|i|is)\b", re.I)


def _requested(text: str) -> Language | None:
    match = _OUTPUT_LANGUAGE.search(unicodedata.normalize("NFC", text))
    return _DECLARED[next(group.lower() for group in match.groups() if group)] if match else None


def for_text(text: str, fallback: str | None = None) -> Language | None:
    text = unicodedata.normalize("NFC", text)
    if _VIETNAMESE.search(text) or _VI_PLAIN.search(text):
        return "vi"
    if _HAN.search(text):
        return "zh"
    if len(_ENGLISH.findall(text)) >= 2:
        return "en"
    return fallback if fallback in NAMES else None


def for_form(form: dict, lang: str | None) -> Language:
    copy = form.get("reference_copy")
    if not copy and not form.get("remake_adaptation"):
        copy = form.get("script")
    inputs = [text for text in (copy, form.get("brief")) if text]
    if requested := next((value for text in inputs if (value := _requested(text))), None):
        return requested
    default = speech_language(lang)
    return default if default == "vi" else for_text("\n".join(inputs), default)


def speech_hint(language: Language | None, line: str) -> str:
    if not line:
        return ""
    return f"只用 {NAMES[for_text(line, language) or 'zh']} 清楚地说指定台词。"


def direction(language: Language) -> str:
    return (
        f"本次成片语言是{NAMES[language]}。用户可见的分镜说明、镜头标题、对白、字幕、旁白、片名和片尾文案使用此语言。"
        "保留品牌名和业主指定的逐字台词，模板示例和界面语言不改变成片语言。"
    )
