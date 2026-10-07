"""Fixed phrases and the response-level instructions that make the front desk say them.

Nothing is synthesized here. ``response.create`` with ``response.instructions``
makes the realtime model say the text in the call's own voice: measured
2026-10-07, the greeting started 0.56 s after the request and word for word,
"still working" 0.62 s without calling the tool again, and a 58-character
result 0.57 s, word for word (docs/PERSONAL_ASSISTANT_VOICE_PLAN.md §2).
"""
LANGS = ("zh", "en")

PHRASES = {
    "greeting": {"zh": "嗨，我在，你说。", "en": "Hi, I'm here. Go ahead."},
    "still_working": {"zh": "还在办，好了我马上告诉你。", "en": "Still on it. I'll tell you as soon as it's done."},
    "result_in_text": {"zh": "办好了，结果我写在对话里了。", "en": "Done. I've put the result in the conversation."},
    "limit_reached": {"zh": "这通电话到时间了，我们文字里继续。",
                      "en": "This call has reached its time limit. Let's continue in text."},
}

# What a turn's function_call_output says when the assistant has no answer to read.
SPEECHES = {
    "timeout": {"zh": "这件事还在办，办好了我在对话里告诉你。",
                "en": "This is still in progress. I'll tell you in the conversation when it's done."},
    "failed": {"zh": "刚才没办成，原因我写在对话里了。",
               "en": "That didn't work out. I've put the reason in the conversation."},
    "unavailable": {"zh": "我这边没能交给个人助理，你稍后再说一次吧。",
                    "en": "I couldn't pass that to the assistant. Please say it again in a moment."},
}

FOUND = {"zh": "我这边查到了，", "en": "Here's what I found. "}
_SAY = {"zh": "只说这一句，不要调用任何工具，不要加别的话：",
        "en": "Say only this one sentence, call no tools and add nothing else: "}
_VERBATIM = {"zh": "请逐字朗读下面这段话，一个字都不要增减或改写，不要调用工具：",
             "en": "Read the following aloud word for word, without adding, removing or rewording anything, "
                   "and call no tools: "}
_RESULT = {"zh": "个人助理的结果回来了。", "en": "The personal assistant's result is back. "}


def _lang(lang: str) -> str:
    return lang if lang in LANGS else "zh"


def phrase_text(key: str, lang: str) -> str:
    if key not in PHRASES:
        raise KeyError(f"unknown phrase: {key}")
    return PHRASES[key][_lang(lang)]


def speech_text(key: str, lang: str) -> str:
    return SPEECHES[key][_lang(lang)]


def phrase_instructions(key: str, lang: str) -> str:
    return _SAY[_lang(lang)] + phrase_text(key, lang)


def delivery_instructions(speech: str, lang: str) -> str:
    """A result read word for word, opened with "我这边查到了" as the session prompt promises."""
    lang = _lang(lang)
    return _RESULT[lang] + _VERBATIM[lang] + FOUND[lang] + speech


def notice_instructions(speech: str, lang: str) -> str:
    """A timeout or failure notice: verbatim too, but nothing was found, so no "查到了"."""
    return _VERBATIM[_lang(lang)] + speech


async def user_language(user_id: str) -> str:
    """The UI language the web and mobile clients store as ``extra.locale``; Chinese by default."""
    from db.repository.preference_repo import PgPreferenceRepo
    try:
        locale = str(((await PgPreferenceRepo().get(user_id)) or {}).get("extra", {}).get("locale") or "")
    except Exception:  # a missing preference row or a read failure: the default
        return "zh"
    return "en" if locale.lower().startswith("en") else "zh"
