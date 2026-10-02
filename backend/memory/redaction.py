"""Bounded diagnostic values: redact before persistence and again on reads."""
import hashlib
import json
import os
import re

_SENSITIVE = re.compile(r"(?:password|passwd|secret|api.?key|authorization|credential|access.?token|refresh.?token)", re.I)
_CREDENTIAL_ENV = re.compile(r"(?:_KEY$|_TOKEN$|^BAILIAN_KEY$|^JEV_KEY$)", re.I)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def redact_text(text: str, limit: int = 1200) -> str:
    # Never persist known runtime credentials even if a provider echoes them.
    for name, value in os.environ.items():
        if (_SENSITIVE.search(name) or _CREDENTIAL_ENV.search(name)) and len(value) >= 8:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(r"\b(?:sk-|jv_live_|ts_live_)[A-Za-z0-9_-]{8,}", "[redacted]", text)
    text = re.sub(r"(?i)(password|api[_ -]?key|secret|token|密码|密钥)\s*[:=：]\s*[^\s,;，；]+", r"\1=[redacted]", text)
    text = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[email redacted]", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[phone redacted]", text)
    return text[:limit] + ("…[truncated]" if len(text) > limit else "")


def redact_value(value, *, limit: int = 1200, depth: int = 0):
    if depth > 8:
        return "[depth limited]"
    if isinstance(value, str):
        return redact_text(value, limit)
    if isinstance(value, dict):
        return {str(key): "[redacted]" if _SENSITIVE.search(str(key)) else redact_value(item, limit=limit, depth=depth + 1)
                for key, item in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [redact_value(item, limit=limit, depth=depth + 1) for item in value[:100]]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return redact_text(str(value), limit)


def json_hash(value) -> str:
    return text_hash(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
