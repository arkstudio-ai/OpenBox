"""Server → client events of /ws/assistant/voice (docs/VOICE_CALL_SPEC.md §5.3).

Every JSON event the socket sends is built here, so names and fields cannot
drift from the contract the web and mobile clients are built against.
Unknown values raise: a typo must fail a test, not reach a client.
"""
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000

PHASES = frozenset({"greeting", "listening", "thinking", "speaking", "working"})
# A reply of ours starts: the free greeting, a progress update, or a fixed notice. Clients only log the key.
PHRASE_KEYS = frozenset({"greeting", "progress", "result_in_text", "limit_reached"})
TURN_STATES = frozenset({"accepted", "working", "late", "delivered", "timeout", "failed"})
LIMIT_REASONS = frozenset({"max_duration", "daily_quota"})
END_REASONS = frozenset({"hangup", "error", "limit", "quota", "concurrent", "mic_lost", "network",
                         "unsupported", "mic_denied", "mic_missing", "mic_busy"})
# Shown to the user as is. Never a provider message, key or identifier.
ERROR_MESSAGES = {
    "provider_unavailable": {"zh": "没能接通，请稍后再试。", "en": "Could not connect. Try again later."},
    "provider_error": {"zh": "通话中断了，请稍后再试。", "en": "The call dropped. Try again later."},
    "assistant_unavailable": {"zh": "先打开个人助理，再打电话。", "en": "Open the personal assistant first, then call."},
    "bad_frame": {"zh": "音频数据不正确，通话已结束。", "en": "The audio data was not valid, so the call ended."},
    "internal": {"zh": "通话中断了，请稍后再试。", "en": "The call dropped. Try again later."},
}


def _member(value, allowed, name):
    if value not in allowed:
        raise ValueError(f"unknown {name}: {value!r}")
    return value


def ready(call_id: str, model: str, max_seconds: int, price_date: str) -> dict:
    return {"type": "ready", "call_id": call_id, "model": model, "input_sample_rate": INPUT_SAMPLE_RATE,
            "output_sample_rate": OUTPUT_SAMPLE_RATE, "max_seconds": int(max_seconds), "price_date": price_date}


def phase(value: str, *, working: bool, late: bool) -> dict:
    return {"type": "phase", "value": _member(value, PHASES, "phase"), "working": bool(working), "late": bool(late)}


def playback_clear() -> dict:
    return {"type": "playback.clear"}


def phrase(key: str) -> dict:
    return {"type": "phrase", "key": _member(key, PHRASE_KEYS, "phrase")}


def turn(turn_id: str, state: str, *, inbox_id: str | None, message_id: str | None) -> dict:
    return {"type": "turn", "turn_id": turn_id, "state": _member(state, TURN_STATES, "turn state"),
            "inbox_id": inbox_id, "message_id": message_id}


def cost(snapshot: dict) -> dict:
    """``CallMeter.snapshot()`` already has the event's shape."""
    return {**snapshot, "type": "cost"}


def heartbeat(elapsed_seconds: float) -> dict:
    return {"type": "heartbeat", "elapsed_seconds": int(elapsed_seconds)}


def limit(reason: str, elapsed_seconds: float) -> dict:
    return {"type": "limit", "reason": _member(reason, LIMIT_REASONS, "limit reason"),
            "elapsed_seconds": int(elapsed_seconds)}


def error(code: str, lang: str) -> dict:
    texts = ERROR_MESSAGES[code]
    return {"type": "error", "code": code, "message": texts.get(lang, texts["zh"])}


def ended(reason: str, *, duration_seconds: float, pending_turns: int, cost_snapshot: dict) -> dict:
    return {"type": "ended", "reason": _member(reason, END_REASONS, "end reason"),
            "duration_seconds": int(duration_seconds), "pending_turns": int(pending_turns),
            "cost": cost(cost_snapshot)}
