"""Public ↔ storage id mapping.

Storage ids are ``<kind>_<ULID>`` (``session_``, ``message_``, ``part_``,
``asset_``); question checkpoints use a bare ULID. The public API shows the
short prefixes from the contract instead. The mapping is a pure rename, so
nothing needs to be stored.

One id has no row of its own: the *assistant turn*. Internally each LLM step
is a separate assistant message, all pointing at the user message that
started the turn. The public API folds them into one assistant message whose
id is the user message's ULID plus one — deterministic, so it can be handed
out in the ``202`` before the loop has run a single step, and it sorts right
after the user message it answers.
"""
from __future__ import annotations

from ulid import ULID

_PUBLIC_BY_KIND = {
    "session": "ses",
    "message": "msg",
    "part": "prt",
    "asset": "fil",
    "question": "qst",
}
_KIND_BY_PUBLIC = {v: k for k, v in _PUBLIC_BY_KIND.items()}


def public_id(internal: str | None, kind: str | None = None) -> str | None:
    """``session_01J…`` → ``ses_01J…``. Unknown shapes pass through."""
    if not internal:
        return internal
    if kind == "question" and "_" not in internal:
        return f"qst_{internal}"
    prefix, _, rest = internal.partition("_")
    short = _PUBLIC_BY_KIND.get(prefix)
    if short is None or not rest:
        return internal
    return f"{short}_{rest}"


def internal_id(public: str | None, kind: str) -> str | None:
    """``ses_01J…`` → ``session_01J…``; questions map back to the bare ULID."""
    if not public:
        return public
    expected = _PUBLIC_BY_KIND[kind]
    prefix, _, rest = public.partition("_")
    if prefix != expected or not rest:
        # Tolerate an internal id: the same endpoints serve JWT debugging.
        return public
    if kind == "question":
        return rest
    return f"{kind}_{rest}"


def _ulid_of(internal: str) -> str:
    return internal.partition("_")[2] or internal


def assistant_turn_id(user_message_id: str) -> str:
    """Public id of the folded assistant reply to ``user_message_id``."""
    ulid = ULID.from_str(_ulid_of(user_message_id))
    return f"msg_{ULID.from_int(int(ulid) + 1)}"
