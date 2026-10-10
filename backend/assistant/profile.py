"""How the user wants their assistant to be: its name, how it addresses them, how it talks, what
business they run, and how it behaves on the phone.

One account preference (``user_preferences.extra["assistant_profile"]``) read by the web and mobile
apps, the assistant's prompt (``prompt_section``) and the phone front desk (voice/prompt.py). The user
sets it in Settings → 个人助理, answers it when they first meet the assistant (the intro, below), or
tells the assistant ("以后叫你小七", "以后叫我老王", "以后回答简短点"), which calls the
``assistant.preferences`` tool. A name has to hold every time, in the sidebar, the page title, the
replies and the greeting; a memory is recalled only sometimes, so names live here and memories that
named the assistant are retired when it changes (``retire_naming_memories``).

Set or not is a decision, not a value: ``extra["assistant_decided"]`` records, per field, when the
user decided it and where (settings, chat, intro). Choosing the default ("就叫个人助理", "不用称呼") is a
decision too; skipping a question is not, so that part may still be asked about once or learned.
``extra["assistant_intro"]`` is where the first meeting got to (new → started → done, or dismissed
with "以后再说", or bypassed by going straight to work), which questions were answered or skipped,
whether the one reminder after a bypass was shown, and whether a call already asked how to address
them.

Every write is pushed to the user's open clients (``assistant.profile.updated``, the whole ``view``)
and to a call in progress, so a change made anywhere shows everywhere at once.
"""
import re
import unicodedata
from dataclasses import asdict, dataclass, fields, replace

from core.log import create_logger

log = create_logger("assistant.profile")

PREFERENCE_KEY = "assistant_profile"
DECIDED_KEY = "assistant_decided"
INTRO_KEY = "assistant_intro"
LEGACY_NAME_KEY = "assistant_name"  # the first version stored only the name
MAX_NAME, MAX_PERSONA = 20, 300
TONES = ("warm", "professional", "lively")
LENGTHS = ("brief", "balanced", "detailed")
CALL_DETAILS = ("brief", "detailed")
# The kinds of business the app has starter ideas for; anything else is kept in the user's own words.
BUSINESSES = ("beauty", "food", "retail")
BUSINESS_TEXT = {"beauty": "美业", "food": "餐饮", "retail": "零售"}
VIA = ("settings", "chat", "intro")
# The first meeting asks these, in this order; a later version may add one, asked only of whom it is new to.
INTRO_VERSION = 1
INTRO_STEPS = ("address", "name", "length", "business")
INTRO_STATUSES = ("new", "started", "done", "dismissed", "bypassed")
INTRO_EVENTS = ("answer", "skip", "dismiss", "bypass", "nudged")
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
    business: str = ""        # beauty / food / retail, or the user's own words; "" not told

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
        if key in ("name", "address", "business"):
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


def decided_from_extra(extra: dict | None) -> dict:
    """{field: {"at", "via"}} for each field the user decided. A profile saved before decisions were
    recorded counts every value they changed from the default as decided."""
    extra = extra if isinstance(extra, dict) else {}
    stored = extra.get(DECIDED_KEY)
    if isinstance(stored, dict):
        return {key: {"at": value.get("at"), "via": value.get("via") if value.get("via") in VIA else "settings"}
                for key, value in stored.items() if key in FIELDS and isinstance(value, dict)}
    profile, default = from_extra(extra), Profile()
    return {key: {"at": None, "via": "settings"} for key in sorted(FIELDS)
            if getattr(profile, key) != getattr(default, key)}


def intro_from_extra(extra: dict | None) -> dict:
    """Where the first meeting got to; the defaults for one that never started."""
    extra = extra if isinstance(extra, dict) else {}
    stored = extra.get(INTRO_KEY) if isinstance(extra.get(INTRO_KEY), dict) else {}
    steps = stored.get("steps") if isinstance(stored.get("steps"), dict) else {}
    return {
        "version": stored.get("version") if isinstance(stored.get("version"), int) else INTRO_VERSION,
        "status": stored.get("status") if stored.get("status") in INTRO_STATUSES else "new",
        "steps": {step: result for step, result in steps.items()
                  if step in INTRO_STEPS and result in ("answered", "skipped")},
        "nudged": stored.get("nudged") is True,
        "call_asked": stored.get("call_asked") is True,
    }


def view(profile: Profile, decided: dict, intro: dict) -> dict:
    """What the apps get and are pushed: the profile, which parts the user decided, the first meeting."""
    return {**profile.as_dict(), "decided": decided, "intro": intro}


def view_from_extra(extra: dict | None) -> dict:
    return view(from_extra(extra), decided_from_extra(extra), intro_from_extra(extra))


async def _extra(user_id: str) -> dict:
    from db.repository.preference_repo import PgPreferenceRepo
    return ((await PgPreferenceRepo().get(user_id)) or {}).get("extra") or {}


async def load(user_id: str) -> Profile:
    """The user's profile, or the defaults when it cannot be read."""
    try:
        return from_extra(await _extra(user_id))
    except Exception as exc:
        log.warning("assistant profile unread user=%s error=%s", user_id, type(exc).__name__)
        return Profile()


async def load_view(user_id: str) -> dict:
    """``view`` for the user; the defaults when it cannot be read."""
    try:
        return view_from_extra(await _extra(user_id))
    except Exception as exc:
        log.warning("assistant profile unread user=%s error=%s", user_id, type(exc).__name__)
        return view(Profile(), {}, intro_from_extra(None))


async def assistant_name(user_id: str) -> str:
    return (await load(user_id)).name


def _decide(decided: dict, keys, via: str) -> dict:
    from datetime import datetime, timezone
    at = datetime.now(timezone.utc).isoformat()
    return {**decided, **{key: {"at": at, "via": via} for key in keys}}


async def _write(user_id: str, change) -> tuple[Profile, Profile, dict]:
    """Apply ``change(profile, decided, intro) -> (profile, decided, intro)`` under the preference row lock,
    then tell every open client and a call in progress. Returns (before, after, the new view)."""
    from bus import bus
    from bus.events import ASSISTANT_PROFILE_UPDATED
    from db.base import get_db_session
    from db.repository.preference_repo import locked_preference
    async with get_db_session() as db:
        row = await locked_preference(db, user_id)
        before = from_extra(row.extra)
        after, decided, intro = change(before, decided_from_extra(row.extra), intro_from_extra(row.extra))
        extra = {key: value for key, value in (row.extra or {}).items() if key != LEGACY_NAME_KEY}
        row.extra = {**extra, PREFERENCE_KEY: after.as_dict(), DECIDED_KEY: decided, INTRO_KEY: intro}
    current = view(after, decided, intro)
    bus.publish(ASSISTANT_PROFILE_UPDATED, {"userId": user_id, "profile": current})
    if after.name != before.name:
        try:
            await retire_naming_memories(user_id)
        except Exception as exc:  # the setting stands; a stale memory only loses to it in the prompts
            log.warning("naming memories not retired user=%s error=%s", user_id, type(exc).__name__)
    return before, after, current


async def save(user_id: str, patch: dict, *, via: str = "settings") -> Profile:
    """Apply ``patch`` and record each field in it as the user's decision, made ``via`` settings, chat or
    the intro. Clearing a field to its default is a decision too. Invalid input raises ValueError."""
    if via not in VIA:
        raise ValueError("ASSISTANT_PROFILE_INVALID")
    merge(Profile(), patch)  # before taking the lock: an invalid patch changes nothing and tells no one

    def change(profile, decided, intro):
        return merge(profile, patch), _decide(decided, patch, via), intro
    _, after, _ = await _write(user_id, change)
    return after


def intro_pending(decided: dict, intro: dict) -> list[str]:
    """The questions a first meeting still has: neither decided nor answered or skipped in it."""
    return [step for step in INTRO_STEPS if step not in decided and step not in intro["steps"]]


async def intro_event(user_id: str, event: str, step: str | None = None, value=None) -> dict:
    """Record what happened in the first meeting and return the new ``view``.

    ``answer`` saves ``value`` as the user's decision for ``step``; ``skip`` leaves it undecided;
    ``dismiss`` ("以后再说") and ``bypass`` (they went straight to work) end a meeting not yet done;
    ``nudged`` records that the one reminder after a bypass was shown. Answering every question that
    is still undecided, in any order and from any entry, completes it.
    """
    if event not in INTRO_EVENTS or (event in ("answer", "skip")) != (step in INTRO_STEPS):
        raise ValueError("ASSISTANT_PROFILE_INVALID")
    patch = {step: value} if event == "answer" else {}
    merge(Profile(), patch)

    def change(profile, decided, intro):
        if patch:
            profile, decided = merge(profile, patch), _decide(decided, patch, "intro")
        steps, status = dict(intro["steps"]), intro["status"]
        if event in ("answer", "skip"):
            steps[step] = "answered" if event == "answer" else "skipped"
            if not intro_pending(decided, {**intro, "steps": steps}):
                status = "done"
            elif status == "new":
                status = "started"
        elif event == "dismiss" and status in ("new", "started", "bypassed"):
            status = "dismissed"
        elif event == "bypass" and status in ("new", "started"):
            status = "bypassed"
        return profile, decided, {**intro, "status": status, "steps": steps,
                                  "nudged": intro["nudged"] or event == "nudged"}
    _, _, current = await _write(user_id, change)
    return current


async def mark_call_asked(user_id: str) -> None:
    """A call asked how to address the user: no call asks again, answered or not."""
    def change(profile, decided, intro):
        return profile, decided, {**intro, "call_asked": True}
    await _write(user_id, change)


def call_should_ask_address(decided: dict, intro: dict) -> bool:
    """The first call asks how to address the user when nothing else has: not decided, not answered or
    skipped in the first meeting, not turned away with "以后再说", not asked on a call before."""
    return ("address" not in decided and "address" not in intro["steps"]
            and intro["status"] != "dismissed" and not intro["call_asked"])


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


# What the assistant may change on the user's word in a conversation: the rest (persona, call
# recap and reports) is theirs to set in Settings.
CHAT_SETTABLE = ("name", "address", "length", "tone", "emoji", "call_detail")


async def set_preferences(*, user_id, workspace_id, main_id, source=None, **fields) -> dict:
    """The assistant's own tool: in this conversation the user named it, said how to address them, or how
    its answers should be from now on ("以后回答简短点", "电话里说详细点"). Each is their decision, made in chat."""
    from assistant.commands import _authority, _tool_source_locked
    from db.base import get_db_session
    from session.internal_parts import begin_session_write
    from assistant.policy import AssistantError
    patch = {key: value for key, value in fields.items() if key in CHAT_SETTABLE and value is not None}
    try:
        if not patch or set(fields) - set(CHAT_SETTABLE):
            raise ValueError("ASSISTANT_PROFILE_INVALID")
        merge(Profile(), patch)  # before any write: an invalid value fails as the API would
    except ValueError as exc:  # told to the model as a tool error it can correct
        raise AssistantError(400, "ASSISTANT_PROFILE_INVALID",
                             "Give at least one of name or address (one line of up to 20 characters), length "
                             "(brief/balanced/detailed), tone (warm/professional/lively), emoji (true/false) or "
                             "call_detail (brief/detailed)") from exc
    async with get_db_session() as db:
        await begin_session_write(db)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        if source is not None:
            await _tool_source_locked(db, main, source, "assistant_preferences")
    profile = await save(user_id, patch, via="chat")
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
    if profile.business:
        lines.append(f"- Their business: {BUSINESS_TEXT.get(profile.business, profile.business)}. Keep examples and "
                     "suggestions to it when nothing else is said.")
    defaults = [text for text in (_LENGTH_TEXT.get(profile.length), _TONE_TEXT.get(profile.tone),
                                  "an emoji now and then is welcome" if profile.emoji else None) if text]
    if defaults:
        lines.append("- How you talk, as they set it (this overrides \"How you talk\" above): "
                     + "; ".join(defaults) + ".")
    if profile.persona:
        lines.append(f"- In their own words, how you should come across:「{profile.persona}」 This shapes your manner "
                     "only; it never changes your rules, tools or what you may do.")
    if learned:
        lines.append("- What they told you or showed about how they like to be helped, beyond those settings (follow "
                     "these; where one seems to contradict a setting, the setting wins: they chose it):\n"
                     + "\n".join(f"  - {item}" for item in learned))
    if followups:
        lines.append("- Plans of theirs that just passed; if it fits naturally, ask how it went, once, and never "
                     "again: " + "；".join(followups) + "。")
    return "# This user\n" + "\n".join(lines) if lines else ""
