"""Bounded diagnostic values: redact before persistence and again on reads."""
from functools import lru_cache
import hashlib
import json
import os
import re

_SENSITIVE = re.compile(r"(?:password|passwd|secret|api.?key|authorization|credential|access.?token|refresh.?token)", re.I)
_CREDENTIAL_ENV = re.compile(r"(?:_KEY$|_TOKEN$|^BAILIAN_KEY$|^JEV_KEY$)", re.I)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# Credentials, and contact or identity numbers. Memory never stores them and
# never sends them to a model; everything else in the same text still counts.
CREDENTIAL = re.compile(r"(?:\b(?:sk-[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{16})\b|-----BEGIN [A-Z ]*PRIVATE KEY-----"
                        r"|(?:api[_ -]?key|password|密码|密钥|access[_ -]?token)\s*[:=：]\s*\S{6,})", re.I)
# Lookarounds, not \b: next to Chinese text there is no word boundary.
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z])")
_MOBILE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_LANDLINE = re.compile(r"(?<![\d-])0\d{2,3}-\d{7,8}(?![\d-])")
_ID_NUMBER = re.compile(r"(?<![0-9A-Za-z])[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]"
                        r"(?![0-9A-Za-z])")
_DIGIT_RUN = re.compile(r"(?<![\d-])\d(?:[ -]?\d){14,18}(?![\d-])")
_CARD_HINT = re.compile(r"卡号|银行卡|信用卡|储蓄卡|借记卡|工资卡|账号|帐号|账户|帐户|card|account|iban", re.I)
# Door-level detail of an address: unit, building or room numbers, or a home's
# street number. A city or district is fine to remember.
_ADDRESS = re.compile(r"\d+\s*(?:单元|号楼|栋|幢)|\d{2,5}\s*室|(?:家|住|住址|地址).{0,24}?(?:路|街|道|巷|弄|胡同|里|村)\s*\d+\s*号")


def _luhn(digits: str) -> bool:
    total = 0
    for position, digit in enumerate(reversed(digits)):
        value = int(digit) * (2 if position % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def _card_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for match in _DIGIT_RUN.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if _ID_NUMBER.fullmatch(digits):
            continue
        if _luhn(digits) or _CARD_HINT.search(text[max(0, match.start() - 16):match.start()]):
            spans.append(match.span())
    return spans


@lru_cache(maxsize=1024)
def _credential_name(name: str) -> bool:
    return bool(_SENSITIVE.search(name) or _CREDENTIAL_ENV.search(name))


def _runtime_credentials() -> list[str]:
    """Current values of credential-named environment variables, in order."""
    return [value for name, value in os.environ.items() if _credential_name(name) and len(value) >= 8]


def _mask_credentials(text: str, ranges: list[list[int]] | None = None, credentials: list[str] | None = None) -> str:
    def substitute(pattern, replacement, value):
        if ranges is None:
            return re.sub(pattern, replacement, value)
        # Map original excerpt boundaries through the same replacements as the
        # complete text. An excerpt cutting a credential receives the entire
        # replacement, never a surviving prefix/suffix of the secret.
        matches = list(re.finditer(pattern, value))
        for span in ranges:
            start, end = span
            shift = 0
            for match in matches:
                replacement_size = len(match.expand(replacement))
                delta = replacement_size - (match.end() - match.start())
                if match.end() <= start:
                    span[0] += delta
                elif match.start() < start < match.end():
                    span[0] = match.start() + shift
                if match.end() <= end:
                    span[1] += delta
                elif match.start() < end < match.end():
                    span[1] = match.start() + shift + replacement_size
                shift += delta
        return re.sub(pattern, replacement, value)

    # Never persist known runtime credentials even if a provider echoes them.
    for value in _runtime_credentials() if credentials is None else credentials:
        text = (text.replace(value, "[redacted]") if ranges is None
                else substitute(re.escape(value), "[redacted]", text))
    text = substitute(r"(?i)bearer\s+\S+", "Bearer [redacted]", text)
    text = substitute(r"\b(?:sk-|jv_live_|ts_live_)[A-Za-z0-9_-]{8,}", "[redacted]", text)
    return substitute(r"(?i)(password|api[_ -]?key|secret|token|密码|密钥)\s*[:=：]\s*[^\s,;，；]+", r"\1=[redacted]", text)


def redact_credentials(text: str) -> str:
    """Credential-only projection for authorized original task evidence.

    Unlike a persistent memory profile, a task may need the contact details
    its user supplied. Do not silently turn those instructions into redactions.
    """
    return _mask_credentials(text)


def redact_credential_ranges(text: str, ranges: list[tuple[int, int]]) -> list[str]:
    """Project bounded original excerpts after redacting their whole context."""
    if any(type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
           for start, end in ranges):
        raise ValueError("Invalid credential projection range")
    mapped = [[start, end] for start, end in ranges]
    redacted = _mask_credentials(text, mapped)
    return [redacted[start:end] for start, end in mapped]


def _mask(text: str, credentials: list[str] | None = None) -> str:
    text = _mask_credentials(text, credentials=credentials)
    text = _EMAIL.sub("[email redacted]", text)
    text = _ID_NUMBER.sub("[id number redacted]", text)
    for start, end in reversed(_card_spans(text)):
        text = text[:start] + "[card number redacted]" + text[end:]
    text = _MOBILE.sub("[phone redacted]", text)
    return _LANDLINE.sub("[phone redacted]", text)


def mask_sensitive(text: str) -> str:
    """What a memory model may see of a text: sensitive values masked, nothing cut."""
    return _mask(text)


def sensitive_kind(text: str) -> str | None:
    """Why a text must not become a memory, or None when it may."""
    if CREDENTIAL.search(text) or _mask_credentials(text) != text:
        return "credential"
    if _ID_NUMBER.search(text):
        return "id_number"
    if _card_spans(text):
        return "card_number"
    if _MOBILE.search(text) or _LANDLINE.search(text):
        return "phone"
    if _EMAIL.search(text):
        return "email"
    if _ADDRESS.search(text):
        return "address"
    return None


def redact_text(text: str, limit: int = 1200, *, credentials: list[str] | None = None) -> str:
    text = _mask(text, credentials)
    return text[:limit] + ("…[truncated]" if len(text) > limit else "")


def redact_value(value, *, limit: int = 1200, depth: int = 0, credentials: list[str] | None = None):
    # The environment is read once per value, not once per string inside it.
    credentials = _runtime_credentials() if credentials is None else credentials
    if depth > 8:
        return "[depth limited]"
    if isinstance(value, str):
        return redact_text(value, limit, credentials=credentials)
    if isinstance(value, dict):
        return {str(key): "[redacted]" if _SENSITIVE.search(str(key))
                else redact_value(item, limit=limit, depth=depth + 1, credentials=credentials)
                for key, item in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [redact_value(item, limit=limit, depth=depth + 1, credentials=credentials) for item in value[:100]]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return redact_text(str(value), limit, credentials=credentials)


def json_hash(value) -> str:
    return text_hash(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
