"""Turn an assistant reply into something the front desk can read aloud (pure functions).

Voice turns already ask the assistant for plain speech; this is the second
line of defence for what it still writes: link targets, list and heading
marks, code marks, and identifiers nobody can say on the phone.
"""
import re

LIMIT = 300
MORE = {"zh": "详细的我写在对话里了。", "en": "The details are in the conversation."}
_NOT_ALNUM_BEFORE, _NOT_ALNUM_AFTER = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"

_FENCE = re.compile(r"^\s*(```|~~~).*$", re.MULTILINE)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_REF_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_AUTOLINK = re.compile(r"<(?:https?://|mailto:)[^>\s]*>")
_URL = re.compile(r"(?:https?://|www\.)[^\s，。！？；、）)\]」』]+")
_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_QUOTE = re.compile(r"^\s*>+\s?", re.MULTILINE)
_LIST = re.compile(r"^\s*(?:[-*+•·]|\d{1,3}[.)、]|[a-zA-Z][.)])\s+", re.MULTILINE)
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$", re.MULTILINE)
_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_EMPHASIS = re.compile(r"(?<![\w*])([*_])(?=\S)(.+?)(?<=\S)\1(?![\w*])")
_STRIKE = re.compile(r"~~(.+?)~~")
_UUID = re.compile(_NOT_ALNUM_BEFORE + r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}" + _NOT_ALNUM_AFTER)
# Hashes and hex IDs; the digit keeps a long all-letter word out of it.
_HEX = re.compile(_NOT_ALNUM_BEFORE + r"(?=[a-f]*[0-9])[0-9a-f]{16,}" + _NOT_ALNUM_AFTER)
# ULIDs, with or without an "inbox_"-style prefix.
_ULID = re.compile(_NOT_ALNUM_BEFORE + r"(?:[A-Za-z][A-Za-z0-9]*_)?01[0-9A-HJKMNP-TV-Z]{20,}" + _NOT_ALNUM_AFTER)
_EMPTY_BRACKETS = re.compile(r"[（(]\s*(?:ID|id|编号)?\s*[:：]?\s*[,，]?\s*[)）]")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_SENTENCE_END = "。！？!?；;…"
_CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")


def clean(text: str | None, lang: str = "zh", limit: int = LIMIT) -> str:
    if not text or not text.strip():
        return ""
    text = _FENCE.sub("", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _REF_LINK.sub(r"\1", text)
    text = _AUTOLINK.sub("", text)
    text = _URL.sub("", text)
    text = _TAG.sub("", text)
    text = _TABLE_RULE.sub("", text)
    text = _HEADING.sub("", text)
    text = _QUOTE.sub("", text)
    text = _LIST.sub("", text)
    text = _STRONG.sub(r"\2", text)
    text = _EMPHASIS.sub(r"\2", text)
    text = _STRIKE.sub(r"\1", text)
    text = text.replace("`", "").replace("|", "，")
    for pattern in (_UUID, _HEX, _ULID):
        text = pattern.sub("", text)
    text = _EMPTY_BRACKETS.sub("", text)
    text = _EMOJI.sub("", text)
    return _truncate(_join_lines(text), lang, limit)


def _join_lines(text: str) -> str:
    """One spoken paragraph: each line becomes a sentence, blank lines vanish."""
    sentences = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip(" ，,")
        if not line:
            continue
        if line[-1] not in _SENTENCE_END + "。.，,：:":
            line += "。" if _CJK.search(line) else "."
        sentences.append(line)
    joined = " ".join(sentences)
    # Spaces only matter between Latin words; around CJK text they are noise.
    joined = re.sub(r"\s+(?=[　-〿㐀-鿿＀-￯])", "", joined)
    return re.sub(r"(?<=[　-〿㐀-鿿＀-￯])\s+", "", joined).strip()


def _truncate(text: str, lang: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    ends = [index for index, char in enumerate(head) if char in _SENTENCE_END
            or (char == "." and (index + 1 == len(text) or text[index + 1].isspace()))]
    cut = head[:ends[-1] + 1] if ends else head.rstrip("，, ") + "……"
    more = MORE.get(lang, MORE["zh"])
    return cut + ("" if _CJK.search(more) else " ") + more
