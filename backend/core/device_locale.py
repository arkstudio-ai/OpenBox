"""Validated mobile locale, frozen on accepted Agent input for durable runs."""
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.ui_language import from_preferences, is_language

ORIGIN_KEY = "device_locale"
TIMEZONE_KEY = "timezone"


def valid_timezone(value) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        return ZoneInfo(value).key
    except (ZoneInfoNotFoundError, ValueError):
        return None


@dataclass(frozen=True)
class DeviceLocale:
    language: str | None = None
    timezone: str | None = None

    def origin(self) -> dict:
        return {key: value for key, value in vars(self).items() if value is not None}

    @classmethod
    def from_origin(cls, reference: dict) -> "DeviceLocale | None":
        value = reference.get(ORIGIN_KEY)
        if not isinstance(value, dict):
            return None
        language = value.get("language")
        timezone = valid_timezone(value.get(TIMEZONE_KEY))
        return cls(language if is_language(language) else None, timezone)

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.timezone)) if self.timezone else datetime.now().astimezone()


mobile_request_locale: ContextVar[DeviceLocale | None] = ContextVar("mobile_request_locale", default=None)


async def for_user(user_id: str | None, captured: DeviceLocale | None = None) -> DeviceLocale:
    captured = captured or DeviceLocale()
    if not user_id or captured.language and captured.timezone:
        return captured
    try:
        from db.repository.preference_repo import PgPreferenceRepo
        row = await PgPreferenceRepo().get(user_id)
    except Exception:
        return captured
    extra = (row or {}).get("extra") or {}
    return DeviceLocale(
        captured.language or from_preferences(row),
        captured.timezone or valid_timezone(extra.get(TIMEZONE_KEY) if isinstance(extra, dict) else None),
    )


async def for_turn(user_id: str | None, message) -> DeviceLocale:
    for part in getattr(message, "parts", None) or ():
        if getattr(part, "origin", None) in {"human", "assistant_delegation"}:
            locale = DeviceLocale.from_origin(getattr(part, "origin_ref", None) or {})
            if locale is not None:
                return await for_user(user_id, locale)
    return await for_user(user_id)
