"""Server-driven cards: one pipeline for a structured choice the user makes on a card.

A card is an ordinary durable question (question.question.ask) with two extra
pieces, so everything the question pipeline already guarantees (persistence,
``question.*`` events, drafts, expiry, supersession by a new message, resuming
the suspended tool call) applies unchanged:

- ``Question.detail`` tells clients how to show it: ``kind``, ``present``
  (``sheet`` pops a bottom sheet in the App, ``inline`` stays in the transcript)
  and ``options`` (a stable id, a role and display extras per option, in the
  same order as ``Question.options``). An App that does not know the kind falls
  back to the plain chip card; the answer is still the option label.
- ``continuation["card"]`` tells the server what the answer does: the kind and
  a private payload. The kind's ``apply`` runs inside the same transaction that
  hands the answer to the waiting tool call, so a choice that changes state
  (the conversation's video model, say) is written atomically with it.

A kind registers once: how to apply the answer, whether the voice call may pick
an option on the user's spoken word, and its short spoken label. The voice call
lists and answers registered cards from the registry; nothing else needs a new
branch per kind.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal, NoReturn

CARD_KEY = "card"

#: The tool result handed back to the waiting call: title, output for the
#: model, and metadata merged into the answered tool part.
CardResult = dict[str, Any]
#: ``apply(db, session, row, payload, option)``; ``option`` is None when the
#: user dismissed the card (or picked the cancel option).
ApplyFn = Callable[[Any, Any, Any, dict, "dict | None"], Awaitable[CardResult]]


@dataclass(frozen=True)
class CardKind:
    kind: str
    #: Short label the voice call uses when it lists cards ("换视频模型").
    voice_label: str
    apply: ApplyFn
    #: The call's voice model may answer it, on the user's own spoken words.
    voice_answerable: bool = True
    #: ``sheet`` pops up in the App; ``inline`` stays in the transcript only.
    present: Literal["sheet", "inline"] = "sheet"


@dataclass
class CardOption:
    id: str
    label: str
    description: str = ""
    #: ``cancel`` means "do nothing / keep things as they are".
    role: Literal["choose", "cancel"] = "choose"
    recommended: bool = False
    #: Display-only fields for the App (price per second, model name, ...).
    extra: dict[str, Any] = field(default_factory=dict)


_KINDS: dict[str, CardKind] = {}


def register(card: CardKind) -> CardKind:
    existing = _KINDS.get(card.kind)
    if existing is not None and existing is not card:
        raise ValueError(f"Card kind {card.kind!r} is already registered")
    _KINDS[card.kind] = card
    return card


def get(kind: str | None) -> CardKind | None:
    _load()
    return _KINDS.get(kind or "")


def kinds() -> dict[str, CardKind]:
    _load()
    return dict(_KINDS)


def _load() -> None:
    """Import the modules that register kinds; registration is a side effect of import."""
    import tool.video_model_card  # noqa: F401


def card_of(continuation: dict | None) -> dict | None:
    """The saved card of a question continuation, or None for any other question."""
    continuation = continuation or {}
    card = continuation.get(CARD_KEY)
    if continuation.get("kind") != "question" or not isinstance(card, dict) or not card.get("kind"):
        return None
    return card


def chosen(card: dict, answers: list[list[str]] | None) -> dict | None:
    """The saved option the answer picked; None for no answer or a cancel option."""
    label = (answers or [[]])[0][:1]
    option = next((row for row in card.get("options") or [] if [row["label"]] == label), None)
    if option is None or option.get("role") == "cancel":
        return None
    return option


async def apply(db, session, row) -> tuple[CardResult, list[dict]] | None:
    """The waiting tool call's result and the events to publish after commit; None if not a card.

    A kind's result may carry ``events`` ({"type", "data"} bus frames); they are
    published only once the answer's transaction commits.
    """
    card = card_of(row.continuation)
    if card is None:
        return None
    kind = get(card["kind"])
    if kind is None:
        raise ValueError(f"Unknown card kind {card['kind']!r}")
    option = None if row.status == "rejected" else chosen(card, row.answers)
    result = await kind.apply(db, session, row, card.get("payload") or {}, option)
    events = list(result.pop("events", []))
    result.setdefault("metadata", {})
    result["metadata"]["card"] = {"kind": card["kind"], "option": option["id"] if option else None}
    return result, events


async def ask_card(
    ctx, *, kind: str, header: str, question: str, options: list[CardOption], payload: dict,
    display: dict | None = None, expires_at: datetime | None = None,
) -> NoReturn:
    """File the card for the running tool call and suspend it until the user answers.

    The tool must be listed in question.question.QUESTION_TOOL_CONTINUATIONS
    with continuation "question". Raises QuestionSuspended (control flow).
    """
    from question.question import Question, QuestionOption, ask

    card = get(kind)
    if card is None:
        raise ValueError(f"Unknown card kind {kind!r}")
    if not 2 <= len(options) <= 6 or len({row.label for row in options}) != len(options):
        raise ValueError("A card offers 2 to 6 options with distinct labels")
    detail = {**(display or {}), "kind": kind, "present": card.present,
              "options": [{"id": row.id, "role": row.role, "recommended": row.recommended, **row.extra}
                          for row in options]}
    await ask(
        ctx.session_id,
        [Question(question=question, header=header, multiple=False, custom=False,
                  requires_confirmation=False, detail=detail,
                  options=[QuestionOption(label=row.label, description=row.description) for row in options])],
        tool={"messageID": ctx.message_id, "callID": ctx.part_id},
        user_id=ctx.user_id or "default",
        continuation={"kind": "question", CARD_KEY: {
            "kind": kind, "payload": payload,
            "options": [{"id": row.id, "label": row.label, "role": row.role} for row in options]}},
        expires_at=expires_at,
    )
    raise AssertionError("ask() always suspends")  # pragma: no cover


def voice_answerable(continuation: dict | None) -> bool:
    card = card_of(continuation)
    kind = get(card["kind"]) if card else None
    return bool(kind and kind.voice_answerable)


def voice_label(continuation: dict | None) -> str | None:
    card = card_of(continuation)
    kind = get(card["kind"]) if card else None
    return kind.voice_label if kind else None
