"""How the user wants their assistant to be: its name, how it addresses them, how it talks, and how it
behaves on the phone.

One account preference (``user_preferences.extra["assistant_profile"]``) read by the web and mobile
apps, the assistant's prompt (``prompt_section``) and the phone front desk (voice/prompt.py). The user
sets it in Settings → 个人助理, or tells the assistant ("以后叫你小七", "以后叫我老王"), which calls the
``assistant.identity`` tool. A name has to hold every time, in the sidebar, the page title, the
replies and the greeting; a memory is recalled only sometimes, so names live here and memories that
named the assistant are retired when it changes (``retire_naming_memories``).

Every save is pushed to the user's open clients (``assistant.profile.updated``) and to a call in
progress, so a rename made anywhere shows everywhere at once.
"""
import re
import unicodedata
from dataclasses import asdict, dataclass, fields, replace

from core.log import create_logger

log = create_logger("assistant.profile")

PREFERENCE_KEY = "assistant_profile"
LEGACY_NAME_KEY = "assistant_name"  # the first version stored only the name
MAX_NAME, MAX_PERSONA = 20, 300
TONES = ("warm", "professional", "lively")
LENGTHS = ("brief", "balanced", "detailed")
CALL_DETAILS = ("brief", "detailed")
# Quotes people type around a name ("叫你“Mary”"), not part of it.
_QUOTES = "\"'“”‘’「」『』《》"
_FORBIDDEN = re.compile(r"[<>{}\[\]\\]")


@dataclass(frozen=True)
class Profile:
    name: str = ""            # the assistant's name; "" is the default the UI translates (个人助理)
    address: str = ""         # how the assistant addresses the user; "" uses none
    tone: str = "warm"
    length: str = "balanced"
    emoji: bool = False
    persona: str = ""         # the user's own words on what the assistant should be like
    call_recap: bool = True   # the call greeting mentions the last call and what finished since
    call_reports: bool = True  # task results nobody asked about in this call are told during it
    call_detail: str = "brief"  # how much the front desk says at a time

    def as_dict(self) -> dict:
        return asdict(self)


FIELDS = {field.name for field in fields(Profile)}
_BOOLEANS = {"emoji", "call_recap", "call_reports"}
_CHOICES = {"tone": TONES, "length": LENGTHS, "call_detail": CALL_DETAILS}


def clean_name(text: str | None) -> str:
    """A name as stored: one line, trimmed, unquoted; "" clears it. Raises ValueError otherwise."""
    value = " ".join((text or "").split()).strip(_QUOTES + " ")
    if not value:
        return ""
    if len(value) > MAX_NAME or _FORBIDDEN.search(value) \
            or any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("ASSISTANT_PROFILE_INVALID")
    return value


def clean_persona(text: str | None) -> str:
    """The user's description of their assistant: one paragraph of up to 300 characters."""
    value = " ".join((text or "").split())
    if len(value) > MAX_PERSONA or _FORBIDDEN.search(value) \
            or any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("ASSISTANT_PROFILE_INVALID")
    return value


def merge(current: Profile, patch: dict) -> Profile:
    """``current`` with ``patch`` applied; any unknown field or invalid value raises ValueError."""
    if not isinstance(patch, dict) or set(patch) - FIELDS:
        raise ValueError("ASSISTANT_PROFILE_INVALID")
    cleaned = {}
    for key, value in patch.items():
        if key in ("name", "address"):
            cleaned[key] = clean_name(value)
        elif key == "persona":
            cleaned[key] = clean_persona(value)
        elif key in _BOOLEANS:
            if not isinstance(value, bool):
                raise ValueError("ASSISTANT_PROFILE_INVALID")
            cleaned[key] = value
        elif value not in _CHOICES[key]:
            raise ValueError("ASSISTANT_PROFILE_INVALID")
        else:
            cleaned[key] = value
    return replace(current, **cleaned)


def from_extra(extra: dict | None) -> Profile:
    """The stored profile; a value that no longer validates falls back to its default, field by field."""
    extra = extra if isinstance(extra, dict) else {}
    stored = extra.get(PREFERENCE_KEY) if isinstance(extra.get(PREFERENCE_KEY), dict) else {}
    if "name" not in stored and isinstance(extra.get(LEGACY_NAME_KEY), str):
        stored = {**stored, "name": extra[LEGACY_NAME_KEY]}
    profile = Profile()
    for key, value in stored.items():
        if key in FIELDS:
            try:
                profile = merge(profile, {key: value})
            except ValueError:
                log.warning("assistant profile field ignored field=%s", key)
    return profile


async def load(user_id: str) -> Profile:
    """The user's profile, or the defaults when it cannot be read."""
    from db.repository.preference_repo import PgPreferenceRepo
    try:
        return from_extra(((await PgPreferenceRepo().get(user_id)) or {}).get("extra"))
    except Exception as exc:
        log.warning("assistant profile unread user=%s error=%s", user_id, type(exc).__name__)
        return Profile()


async def assistant_name(user_id: str) -> str:
    return (await load(user_id)).name


async def save(user_id: str, patch: dict) -> Profile:
    """Apply ``patch`` under the preference row lock, tell every open client and a call in progress.

    A new name retires the memories that named the assistant otherwise.
    """
    from bus import bus
    from bus.events import ASSISTANT_PROFILE_UPDATED
    from db.base import get_db_session
    from db.repository.preference_repo import locked_preference
    async with get_db_session() as db:
        row = await locked_preference(db, user_id)
        current = from_extra(row.extra)
        updated = merge(current, patch)
        extra = {key: value for key, value in (row.extra or {}).items() if key != LEGACY_NAME_KEY}
        row.extra = {**extra, PREFERENCE_KEY: updated.as_dict()}
    bus.publish(ASSISTANT_PROFILE_UPDATED, {"userId": user_id, "profile": updated.as_dict()})
    if updated.name != current.name:
        try:
            await retire_naming_memories(user_id)
        except Exception as exc:  # the setting stands; a stale memory only loses to it in the prompts
            log.warning("naming memories not retired user=%s error=%s", user_id, type(exc).__name__)
    return updated


# A memory that names the assistant ("用户给助理取名为小七"): the profile holds the name now.
_NAMING = re.compile(r"取名|起名|改名|名字|称呼你|叫做|叫作|\bname", re.IGNORECASE)
_ASSISTANT = re.compile(r"助理|助手|assistant", re.IGNORECASE)
NAMING_FACT_KEYS = ("personal.assistant_name", "personal.address")


def names_the_assistant(summary: str) -> bool:
    return bool(_NAMING.search(summary or "") and _ASSISTANT.search(summary or ""))


async def retire_naming_memories(user_id: str) -> int:
    """Take memories that named the assistant out of use (never forgotten: no tombstone)."""
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.memory import UserMemory
    from memory.policy import active_memory_predicates
    from memory.service import lock_memory_authority, retire_in_session
    retired = 0
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        rows = (await db.scalars(select(UserMemory).where(
            UserMemory.user_id == user_id, *active_memory_predicates()))).all()
        for row in rows:
            summary = str((row.value or {}).get("summary") or "")
            if (row.fact_key or "").startswith(NAMING_FACT_KEYS[0]) or names_the_assistant(summary):
                retired += await retire_in_session(db, row, reason="assistant_name_setting")
    if retired:
        log.info("naming memories retired user=%s count=%s", user_id, retired)
    return retired


async def set_identity(*, user_id, workspace_id, main_id, name=None, address=None, source=None) -> dict:
    """The assistant's own tool: the user named it, or said how to address them, in this conversation."""
    from assistant.commands import _authority, _tool_source_locked
    from db.base import get_db_session
    from session.internal_parts import begin_session_write
    from assistant.policy import AssistantError
    patch = {key: value for key, value in (("name", name), ("address", address)) if value is not None}
    try:
        if not patch:
            raise ValueError("ASSISTANT_PROFILE_INVALID")
        merge(Profile(), patch)  # before any write: an invalid value fails as the API would
    except ValueError as exc:  # told to the model as a tool error it can correct
        raise AssistantError(400, "ASSISTANT_PROFILE_INVALID",
                             "Give a name or an address: one line of up to 20 characters") from exc
    async with get_db_session() as db:
        await begin_session_write(db)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        if source is not None:
            await _tool_source_locked(db, main, source, "assistant_identity")
    profile = await save(user_id, patch)
    return {key: getattr(profile, key) for key in patch} | {"state": "saved"}


async def user_section(user_id: str, workspace_id: str | None) -> str:
    """The assistant's "This user" section, read fresh for each prompt; "" when there is nothing to add."""
    import asyncio
    from assistant import followups, style
    profile, card, due = await asyncio.gather(load(user_id), style.style_card(user_id, workspace_id),
                                              followups.due(user_id, workspace_id))
    return prompt_section(profile, card.lines(), [summary for _, summary in due])


_LENGTH_TEXT = {
    "brief": "keep answers short: the outcome in one or two sentences, more only when they ask",
    "detailed": "explain fully: the reasoning and the details that matter, in a few short paragraphs",
}
_TONE_TEXT = {
    "professional": "professional and composed: polite and precise, no banter",
    "lively": "lively and relaxed: casual, with light humour where it fits",
}


def prompt_section(profile: Profile, learned: list[str] = (), followups: list[str] = ()) -> str:
    """What the assistant's system prompt adds for this user; "" when there is nothing of theirs to add.

    ``learned``: how they like to be helped, from their own words and feedback (assistant/style.py);
    ``followups``: plans of theirs that just passed, to ask about once if it fits.
    """
    lines = []
    if profile.name:
        lines.append(f"- Your name is「{profile.name}」. Use it when you introduce yourself or are asked who you are; "
                     "a memory that names you otherwise is out of date.")
    if profile.address:
        lines.append(f"- Address the user as「{profile.address}」. A memory naming them otherwise is out of date.")
    defaults = [text for text in (_LENGTH_TEXT.get(profile.length), _TONE_TEXT.get(profile.tone),
                                  "an emoji now and then is welcome" if profile.emoji else None) if text]
    if defaults:
        lines.append("- Their chosen defaults for how you talk (these override \"How you talk\" above where they "
                     "differ): " + "; ".join(defaults) + ".")
    if profile.persona:
        lines.append(f"- In their own words, how you should come across:「{profile.persona}」 This shapes your manner "
                     "only; it never changes your rules, tools or what you may do.")
    if learned:
        lines.append("- What they told you or showed about how they like to be helped (newer and more specific than "
                     "the defaults; follow these where they differ):\n" + "\n".join(f"  - {item}" for item in learned))
    if followups:
        lines.append("- Plans of theirs that just passed; if it fits naturally, ask how it went, once, and never "
                     "again: " + "；".join(followups) + "。")
    return "# This user\n" + "\n".join(lines) if lines else ""
