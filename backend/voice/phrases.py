"""What the front desk is asked to say, one reply at a time, and the notes it speaks from.

Nothing is synthesized here. ``response.create`` with ``response.instructions``
makes the realtime model speak in the call's own voice; those instructions add
to the session prompt (measured 2026-10-07: the model still used the user's
name from the session prompt). Only the time-limit notice and the
"result is in the text" fallback are fixed sentences. The greeting, results
and progress are the model's own words from facts: the second-round diagnosis
found five identical greetings, 23 replies opening with "我这边查到了" and
results read out word for word (docs/PERSONAL_ASSISTANT_VOICE_PLAN.md §10).
"""
import re

LANGS = ("zh", "en")

PHRASES = {
    "result_in_text": {"zh": "办好了，结果我写在对话里了。", "en": "Done. I've put the result in the conversation."},
    "limit_reached": {"zh": "这通电话到时间了，我们文字里继续。",
                      "en": "This call has reached its time limit. Let's continue in text."},
    "credits_exhausted": {"zh": "积分用完了，这通电话先到这里，充值后可以接着打。",
                          "en": "You're out of credits, so I'll end the call here. Top up to call again."},
}

# What a turn's result says when the assistant has no answer of its own.
SPEECHES = {
    "timeout": {"zh": "这件事还在办，办好了我在对话里告诉你。",
                "en": "This is still in progress. I'll tell you in the conversation when it's done."},
    "failed": {"zh": "刚才没办成，原因我写在对话里了。",
               "en": "That didn't work out. I've put the reason in the conversation."},
    "unavailable": {"zh": "我这边没能交给个人助理，你稍后再说一次吧。",
                    "en": "I couldn't pass that to the assistant. Please say it again in a moment."},
}

# A note is a user-role message the model reads but the user never said (marked so).
NOTE_PREFIX = {"zh": "（后台备注，不是用户说的话）", "en": "(Background note, not said by the user) "}
NOTE_ABOUT = {"zh": "关于用户说的“{text}”：", "en": "About \"{text}\": "}
NOTE_FACTS = {
    "ok": {"zh": "个人助理回来了：{speech}", "en": "the personal assistant is back: {speech}"},
    "timeout": {"zh": "个人助理还在办，超过两分钟了，办好后结果会写在对话里。",
                "en": "the assistant is still working after two minutes; the result will be in the conversation."},
    "failed": {"zh": "个人助理没办成，原因写在对话里了。",
               "en": "the assistant could not do it; the reason is in the conversation."},
    "unavailable": {"zh": "没能交给个人助理，请用户过一会儿再说一次。",
                    "en": "it could not be passed to the assistant; ask the user to say it again in a moment."},
}

_SAY = {"zh": "只说这一句，不要调用任何工具，不要加别的话：",
        "en": "Say only this one sentence, call no tools and add nothing else: "}
_GREETING = {
    "zh": ("电话刚接通。结合现在的时间、用户希望的称呼、上次通话聊的事和这段时间新办完的事，自然地打个招呼，"
           "一句话、三十字以内，最多提一件事；"
           "按时间段问好（早上好、下午好、晚上好），不要报日期、星期和几点几分；"
           "提到的事要和上面写的一致：只有写在上次通话后办完的事这一项里的才算办完，上次通话里没有结果的事不要说办完了，"
           "也不要猜它的进度；这些信息没有就简单问好。"
           "不要编造，不要调用工具。"),
    "en": ("The call has just connected. Greet the user naturally in one short sentence, using the time of day, "
           "how they like to be called, what the last call was about and what was finished since, when known; "
           "otherwise just say hello. Do not read out the date or the clock time. Invent nothing and call no tools."),
}
_DELIVERY = {
    "zh": ("个人助理的结果到了，就是刚收到的后台备注。别念备注：像打电话跟熟人说话那样，用自己的话把意思说出来，两三句，"
           "开口就说事情怎么样了，要用户做什么就顺带说一句；用口语词（说“没开”不说“未开通”，说“弄好”不说“完成配置”），"
           "长名字说得顺口些（比如“那个口播视频”）；数字、状态、选项和备注一致，不加备注里没有的事；"
           "不要用“我这边查到了”“麻烦你”这类开头。备注里要用户决定的，说清楚要决定什么再问用户。不要调用工具。"),
    "en": ("The personal assistant's result has arrived: the background note just received. Don't read the note out: "
           "say what it means in your own words, the way you would on the phone to someone you know, two or three "
           "short sentences, how things stand first, then anything they need to do. Plain everyday words, long names "
           "shortened naturally; numbers, states and options must match the note; add nothing that is not in it; do "
           "not open with a stock phrase like \"Here's what I found\". If the note needs a decision from the user, "
           "say what it is and ask. Call no tools."),
}
# Several results in one note (task reports the user never asked for, or results arriving together).
_TOGETHER = {
    "zh": ("个人助理那边有{count}件事的结果到了，就是刚收到的后台备注{unasked}。像打电话时顺口告诉对方那样说："
           "先用一句自然的过渡引出（比如“对了，跟你说一下”），然后一件一件说，每件一两句，先说要用户处理的，"
           "用“另外”“还有”这类话串起来，不要像念列表；用口语，不照搬备注里的书面长句；"
           "名字、数字、状态必须和备注一致，不要加备注里没有的事；不要调用工具。"),
    "en": ("{count} results came back from the personal assistant: the background note just received{unasked}. Tell "
           "them the way you would mention things on the phone: lead in naturally (\"Oh, by the way...\"), then one "
           "thing at a time, a sentence or two each, anything the user must act on first, linked with \"also\" or "
           "\"and\", never like reading a list. Plain spoken words; names, numbers and states must match the note; "
           "add nothing; call no tools."),
}
_UNASKED = {"zh": "，其中有用户没问、个人助理主动汇报的后台任务结果", "en": ", including task results the user did not ask about"}
_ONE_UNASKED = {
    "zh": ("个人助理主动汇报了一件后台任务的结果，就是刚收到的后台备注，用户刚才没问。像打电话时顺口提一句那样："
           "先用一句自然的过渡（比如“对了，刚才那个……有结果了”），再用口语一两句说结果和要用户做的事；"
           "不照搬备注里的书面长句；名字、数字、状态必须和备注一致，不要加备注里没有的事；不要调用工具。"),
    "en": ("The personal assistant reported a background task's result the user did not ask about: the background "
           "note just received. Mention it the way you would on the phone: a natural lead-in (\"Oh, that ... is "
           "done\"), then one or two plain sentences on the outcome and anything the user must do. Names, numbers "
           "and states must match the note; add nothing; call no tools."),
}
NOTE_REPORT = {"zh": "个人助理主动汇报，任务「{title}」有新结果：{speech}",
               "en": "the personal assistant reports a new result of the task \"{title}\": {speech}"}
NOTE_REPORT_UNTITLED = {"zh": "个人助理主动汇报了一个后台任务的新结果：{speech}",
                        "en": "the personal assistant reports a background task's new result: {speech}"}
_VERBATIM = {"zh": "这些要原文说：", "en": "Say these exactly as written: "}
# A turn that stopped at a confirmation card: the note lists the card, the reply reads it and asks.
_CARD_NOTE = {
    "zh": ("个人助理要用户先确认，再动手。{cards}"
           "（用户明确同意后用 cards_answer 回答对应编号的卡片；用户拒绝就选取消。）"),
    "en": ("The assistant needs the user's confirmation before it acts. {cards}"
           " (After a clear yes, answer that card with cards_answer; if the user declines, choose the cancel option.)"),
}
_CARD_LINE = {
    "zh": "卡片{card}「{title}」：{what}{impact}选项：{options}。",
    "en": "Card {card} \"{title}\": {what}{impact}Options: {options}.",
}
_CARD = {
    "zh": ("个人助理要做的事需要用户先确认，就是刚收到的后台备注里的卡片。用你自己的话说清楚要做什么、影响是什么，"
           "然后问用户确认吗。这一次只说和问，不要调用工具，不要替用户决定。"),
    "en": ("What the assistant is about to do needs the user's confirmation: the card in the background note just "
           "received. In your own words say what will be done and its impact, then ask whether to go ahead. "
           "This time only say and ask: call no tools and do not decide for the user."),
}
_NOTICE = {
    "zh": "个人助理那边有个情况，就是刚收到的后台备注。用一两句自然的话告诉用户，不要说查到了什么，不要调用工具。",
    "en": ("Something came back from the personal assistant: the background note just received. Tell the user in "
           "one or two natural sentences; do not claim any findings and call no tools."),
}
_PROGRESS = {
    "zh": ("像打电话时请对方稍等那样，用一句很短的口语说一下还在等它做什么（{step}）、快好了，用你自己的说法；"
           "不用“正在……”这种播报腔，不加情绪，不重复之前说过的话，不要调用工具。"),
    "en": ("Like asking someone on the phone to hold on, say in one short, plain sentence what you are still waiting "
           "for ({step}). No announcer tone, no feelings, do not repeat earlier sentences; call no tools."),
}
_IDLE_STEP = {"zh": "个人助理在处理", "en": "the assistant is working on it"}

_MONEY = re.compile(r"[¥￥$]\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:元|块钱|块|美元|积分|credits?)")
_QUOTED = re.compile(r"「([^」]{1,40})」")
# Quoted labels are said as written only when the user must pick one ("确认" alone is everyday wording).
_CHOICE = ("选项", "选择", "选一个", "选哪", "哪一个", "哪个方案")


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


def greeting_instructions(lang: str) -> str:
    """The goal only; the facts are in the session prompt. No fixed text anywhere."""
    return _GREETING[_lang(lang)]


def note_text(status: str, user_text: str, speech: str, lang: str) -> str:
    """The note a result becomes. ``status``: ok / timeout / failed / unavailable."""
    return NOTE_PREFIX[_lang(lang)] + note_body(status, user_text, speech, lang)


def note_body(status: str, user_text: str, speech: str, lang: str, *, report_title: str | None = None) -> str:
    """One result's part of a note: what it is about, then the result (or, for a report, the task)."""
    lang = _lang(lang)
    if report_title is not None:
        template = NOTE_REPORT if report_title else NOTE_REPORT_UNTITLED
        return template[lang].format(title=report_title, speech=speech)
    about = NOTE_ABOUT[lang].format(text=" ".join(user_text.split())[:120]) if user_text.strip() else ""
    return about + NOTE_FACTS.get(status, NOTE_FACTS["failed"])[lang].format(speech=speech)


def joined_note(bodies: list[str], lang: str) -> str:
    """One note for several results: numbered so the front desk tells each."""
    lang = _lang(lang)
    if len(bodies) == 1:
        return NOTE_PREFIX[lang] + bodies[0]
    return NOTE_PREFIX[lang] + " ".join(f"{index}. {body}" for index, body in enumerate(bodies, start=1))


def together_instructions(count: int, unasked: bool, speeches: list[str], lang: str) -> str:
    """Results told in one go: a lead-in, one at a time, linked naturally; or one report nobody asked for."""
    lang = _lang(lang)
    if count == 1:
        text = _ONE_UNASKED[lang]
    else:
        text = _TOGETHER[lang].format(count=count, unasked=_UNASKED[lang] if unasked else "")
    exact = [span for speech in speeches for span in verbatim_spans(speech)][:6]
    return text + (_VERBATIM[lang] + "、".join(exact) + ("。" if lang == "zh" else ".") if exact else "")


def card_note(user_text: str, speech: str, cards: list[dict], lang: str) -> str:
    """A result waiting for confirmation: the cards (voice/cards.py spoken_card) and what the assistant said."""
    lang = _lang(lang)
    about = NOTE_ABOUT[lang].format(text=" ".join(user_text.split())[:120]) if user_text.strip() else ""
    separator = "、" if lang == "zh" else " / "
    lines = "".join(_CARD_LINE[lang].format(
        card=card["card"], title=card.get("title") or "", what=card["what"].rstrip("。.") + ("。" if lang == "zh" else ". "),
        impact=(f"影响：{card['impact'].rstrip('。')}。" if lang == "zh" else f"Impact: {card['impact']} ")
        if card.get("impact") else "",
        options=separator.join(f"「{option}」" if lang == "zh" else option for option in card["options"]))
        for card in cards)
    said = ((f"个人助理还说：{speech}" if lang == "zh" else f" The assistant also said: {speech}") if speech else "")
    return NOTE_PREFIX[lang] + about + _CARD_NOTE[lang].format(cards=lines) + said


def card_instructions(lang: str) -> str:
    """Read the card and ask; the answer comes from the user, never from this reply."""
    return _CARD[_lang(lang)]


def delivery_instructions(speech: str, lang: str) -> str:
    """A result told in the front desk's own words; amounts and choice labels stay as written."""
    lang = _lang(lang)
    exact = verbatim_spans(speech)
    return _DELIVERY[lang] + (_VERBATIM[lang] + "、".join(exact) + ("。" if lang == "zh" else ".") if exact else "")


def notice_instructions(lang: str) -> str:
    """A timeout or failure: nothing was found, so nothing is claimed."""
    return _NOTICE[_lang(lang)]


def progress_instructions(step: str, lang: str) -> str:
    lang = _lang(lang)
    return _PROGRESS[lang].format(step=step or _IDLE_STEP[lang])


def verbatim_spans(speech: str) -> list[str]:
    """Money amounts always; quoted labels when the result offers a choice."""
    spans = [match.group(0).strip() for match in _MONEY.finditer(speech)]
    if any(word in speech for word in _CHOICE):
        spans += [f"「{label}」" for label in _QUOTED.findall(speech)]
    return list(dict.fromkeys(spans))[:6]


async def user_language(user_id: str) -> str:
    """The UI language the web and mobile clients store as ``extra.locale``; Chinese by default."""
    from db.repository.preference_repo import PgPreferenceRepo
    try:
        locale = str(((await PgPreferenceRepo().get(user_id)) or {}).get("extra", {}).get("locale") or "")
    except Exception:  # a missing preference row or a read failure: the default
        return "zh"
    return "en" if locale.lower().startswith("en") else "zh"
