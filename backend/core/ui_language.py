"""The interface language a request or a user is in (docs/LIGHT_MODE_I18N_PLAN.md §2.1).

One source (the App's language setting), two pipes:

* synchronous: every request carries ``Accept-Language``; the ASGI middleware
  below normalises it onto ``request.state.ui_language`` so routes that render
  copy (the Light home, the opening card) read it from there;
* asynchronous: the App also stores the choice as
  ``user_preferences.extra.ui_language`` (a shallow-merged key next to
  ``ui_mode``) so pushes, autopilot output, voice calls and assistant prompts
  can read it with no request in hand (``ui_language_for``).

Only three values exist. Fallbacks follow §2.2: ``zh-CN`` and ``en-US`` fall
back to nothing, ``vi-VN`` falls back to ``en-US``. An old App sends no header
and has no preference, which reads as ``zh-CN`` so its behaviour is unchanged.
"""
from __future__ import annotations

from typing import Any

LANGS = ("zh-CN", "en-US", "vi-VN")
DEFAULT = "zh-CN"
#: Where a missing string is taken from, in order; a language absent here falls back to nothing.
FALLBACK: dict[str, tuple[str, ...]] = {"vi-VN": ("en-US",)}
#: ``user_preferences.extra`` key, written next to ``ui_mode``.
PREFERENCE_KEY = "ui_language"
HEADER = "accept-language"
STATE_ATTR = "ui_language"


def chain(lang: str) -> tuple[str, ...]:
    """The language itself, then its fallbacks: ``("vi-VN", "en-US")``."""
    return (lang, *FALLBACK.get(lang, ()))


def normalize(header_value: str | None) -> str | None:
    """``Accept-Language`` → one of :data:`LANGS`, or ``None`` when the header is absent or empty.

    The first entry of a q-list is taken (the App sends a single tag; browsers
    put the preferred one first). ``zh*`` → ``zh-CN``, ``vi*`` → ``vi-VN``, any
    other tag (and ``*``) → ``en-US``.
    """
    if header_value is None:
        return None
    first = header_value.split(",", 1)[0].split(";", 1)[0].strip()
    if not first:
        return None
    primary = first.replace("_", "-").split("-", 1)[0].lower()
    if primary == "zh":
        return "zh-CN"
    if primary == "vi":
        return "vi-VN"
    return "en-US"


def is_language(value: Any) -> bool:
    return isinstance(value, str) and value in LANGS


def from_preferences(row: dict | None) -> str | None:
    """The language a preference row chose: ``extra.ui_language``, else the web's ``extra.locale`` normalised.

    The web and Apps before 1.1.0 (106) stored only ``locale``; an account that switched language there
    has chosen one just the same, so pushes, prompts and the Light copy follow it."""
    extra = (row or {}).get("extra") or {}
    if not isinstance(extra, dict):
        return None
    value = extra.get(PREFERENCE_KEY)
    if is_language(value):
        return value
    locale = extra.get("locale")
    return normalize(locale) if isinstance(locale, str) and locale.strip() else None


async def ui_language_for(user_id: str | None) -> str:
    """The language to use for a user with no request in hand (push, autopilot, voice, prompts)."""
    if not user_id:
        return DEFAULT
    try:
        from db.repository.preference_repo import PgPreferenceRepo
        row = await PgPreferenceRepo().get(user_id)
    except Exception:
        return DEFAULT
    return from_preferences(row) or DEFAULT


async def stored_ui_language(user_id: str | None) -> str | None:
    """The stored ``extra.ui_language`` only — ``None`` when the user never chose one (an old App, the web).

    For places where the Chinese default must not be forced on a user who never said: the 专业 assistant's
    language line, which otherwise leaves the model to follow the user's own words."""
    if not user_id:
        return None
    try:
        from db.repository.preference_repo import PgPreferenceRepo
        row = await PgPreferenceRepo().get(user_id)
    except Exception:
        return None
    return from_preferences(row)


async def set_ui_language(user_id: str, lang: str) -> str:
    if not is_language(lang):
        raise ValueError(f"ui_language must be one of {', '.join(LANGS)}")
    from db.repository.preference_repo import PgPreferenceRepo
    await PgPreferenceRepo().upsert(user_id, extra={PREFERENCE_KEY: lang})
    return lang


def request_language(request) -> str | None:
    """What the middleware put on ``request.state`` (``None`` outside an HTTP request or without a header)."""
    if request is None:
        return None
    state = getattr(request, "state", None)
    value = getattr(state, STATE_ATTR, None) if state is not None else None
    return value if is_language(value) else None


async def resolve(user_id: str | None, request=None) -> str:
    """The request's ``Accept-Language`` first; the stored preference when there is no (valid) header; else ``zh-CN``."""
    return request_language(request) or await ui_language_for(user_id)


class UILanguageMiddleware:
    """Pure ASGI middleware: ``scope["state"]["ui_language"]`` = the normalised ``Accept-Language``, or absent.

    ``Request.state`` reads from ``scope["state"]``, so every route and dependency sees the
    same value; WebSocket scopes get it too (the handshake carries the same header).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            raw = next((value for key, value in scope.get("headers") or () if key == HEADER.encode()), None)
            lang = normalize(raw.decode("latin-1", "replace")) if raw is not None else None
            if lang:
                scope.setdefault("state", {})[STATE_ATTR] = lang
        await self.app(scope, receive, send)
