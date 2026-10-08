"""Natural-language replies derive their decision from authenticated human text.

The model selects a request and cites a human message. It never supplies an
approval action or answer. A complete display (the card UI, or a call reading
the whole request aloud) must precede that input, typed or spoken in a call,
and the accepted command retains independently recheckable original evidence.
"""
from dataclasses import dataclass
import json
import re
from types import SimpleNamespace

from sqlalchemy import select

from assistant.commands import _authority, command_digest
from assistant.policy import AssistantError
from assistant.request_display import CHANNELS, DISPLAYED, TTL_SECONDS, accepted_event
from assistant.request_reads import get_request, original
from assistant.results import part_hash
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_driver import AgentDriverState
from db.models.agent_inbox import AgentInboxItem
from db.models.message import Message
from db.models.part import Part
from question import runtime


#: Human turns of the main session: typed, or spoken in a call (voice).
HUMAN_ENTRYPOINTS = frozenset({"assistant_turn", "assistant_voice"})


@dataclass(frozen=True)
class ReplyContext:
    ctx: object
    source_message_id: str


def unavailable():
    return AssistantError(403, "ASSISTANT_REPLY_SOURCE_REQUIRED",
        "A fresh, complete and unambiguous request display followed by a direct human answer is required; use the request card when unclear")


def normalized(text):
    return text.strip().rstrip("。.!！").strip().casefold()


POSITIVE = {"可以", "同意", "确认", "允许", "允许一次", "继续", "是", "好的", "好", "yes", "ok", "okay", "confirm", "allow", "allow once"}
NEGATIVE = {"拒绝", "不可以", "不同意", "不允许", "否", "no", "reject", "deny"}


def decision(value, text):
    """Conservative whole-message interpretation; quoted substrings grant nothing."""
    answer = normalized(text)
    body = value["request"]
    if value["kind"] == "permission":
        if answer in POSITIVE:
            return {"action": "once", "message": None}
        if answer in NEGATIVE:
            return {"action": "reject", "message": None}
        patterns = body.get("always") or body.get("patterns") or []
        scope = f'{body["tool"]} ({", ".join(patterns)})'
        # Scope bytes (including path case and punctuation) must be explicit.
        if text.strip() in {"始终允许 " + scope, "always allow " + scope}:
            return {"action": "always", "message": None}
        raise unavailable()
    questions = body["questions"]
    if any(q.get("allow_attachments") for q in questions):
        # Resource selection needs the authenticated structured card, not names
        # or attachment IDs guessed from another message.
        raise unavailable()
    texts = [text.strip()]
    if len(questions) > 1:
        lines = text.strip().splitlines()
        if len(lines) != len(questions):
            raise unavailable()
        texts = []
        for number, line in enumerate(lines, 1):
            match = re.fullmatch(rf"\s*{number}[.、:：]\s*(.+)", line)
            if not match:
                raise unavailable()
            texts.append(match[1])
    answers = []
    for question, text in zip(questions, texts):
        labels = [option["label"] for option in question.get("options", [])]
        matches = [label for label in labels if normalized(label) == normalized(text)]
        if len(matches) > 1:
            raise unavailable()
        if not matches and normalized(text) in POSITIVE | NEGATIVE:
            family = POSITIVE if normalized(text) in POSITIVE else NEGATIVE
            matches = [label for label in labels if normalized(label) in family]
            if len(matches) > 1:
                raise unavailable()
        if not matches and question.get("multiple"):
            try:
                choices = json.loads(text)
            except ValueError:
                choices = [part.strip() for part in text.split("、")]
            if (isinstance(choices, list) and choices
                    and all(isinstance(c, str) and c in labels for c in choices)
                    and len(set(choices)) == len(choices)):
                matches = choices
        if len(matches) == 1 or question.get("multiple") and matches:
            answers.append(matches)
        elif not matches and question.get("custom", True) and text.strip():
            answers.append([text.strip()])
        else:
            raise unavailable()
    return {"answers": answers, "attachments": None}


async def human_evidence(db, main, inbox):
    message = await db.get(Message, inbox.message_id)
    if (inbox.user_id != main.user_id or inbox.session_id != main.id or inbox.origin != "human"
            or (inbox.origin_ref or {}).get("actor_user_id") != main.user_id
            or (inbox.origin_ref or {}).get("entrypoint") not in HUMAN_ENTRYPOINTS
            or inbox.attachments or message is None or message.role != "user"
            or message.session_id != main.id or message.user_id != main.user_id):
        raise unavailable()
    parts = list((await db.scalars(select(Part).where(Part.message_id == message.id,
        Part.session_id == main.id, Part.user_id == main.user_id).order_by(Part.created_at, Part.id))).all())
    texts = [p for p in parts if p.type == "text" and not p.data.get("ignored")]
    if (len(texts) != 1 or texts[0].data.get("origin") != "human"
            or texts[0].data.get("synthetic") or texts[0].data.get("text") != inbox.prompt):
        raise unavailable()
    return {"inbox_id": inbox.id, "message_id": message.id,
        "part_id": texts[0].id, "content_hash": part_hash(texts[0]),
        "input_hash": command_digest({"prompt": inbox.prompt, "origin_ref": inbox.origin_ref})}


async def display_evidence(db, main, inbox, value):
    from agent.inbox import _request_digest
    saved = (inbox.origin_ref or {}).get("request_context") or {}
    displays = saved.get("displays", [])
    if saved.get("ambiguous") or len(displays) != 1:
        raise unavailable()
    event = await db.get(AgentEvent, displays[0].get("event_id"))
    accepted = await accepted_event(db, main, inbox)
    digest = _request_digest(delivery=inbox.delivery, prompt=inbox.prompt, attachments=inbox.attachments,
        agent=inbox.agent, model=inbox.model, video_model=inbox.video_model,
        video_resolution=inbox.video_resolution, variant=inbox.variant, output_format=inbox.output_format,
        origin=inbox.origin, origin_ref=inbox.origin_ref)
    if (accepted is None or accepted.payload.get("origin") != "human"
            or accepted.payload.get("origin_ref") != inbox.origin_ref
            or accepted.payload.get("request_digest") != inbox.request_digest
            or digest != inbox.request_digest
            or event is None or event.kind != DISPLAYED or event.session_id != main.id
            or event.payload.get("channel") not in {None, *CHANNELS}
            or event.user_id != main.user_id or command_digest(event.payload) != displays[0].get("digest")
            or event.payload.get("digest") != command_digest(original(value))
            or event.sequence >= accepted.sequence
            or not 0 <= (runtime.utc(accepted.created_at) - runtime.utc(event.created_at)).total_seconds() <= TTL_SECONDS):
        raise unavailable()
    return {"display": displays[0], "accepted_input": {
        "event_id": accepted.id, "digest": command_digest(accepted.payload)}}


async def call_evidence(db, main, ctx, value, human_id):
    from assistant.reporting import _read_call
    await _read_call(db, main, ctx, "requests.reply")
    call = await db.get(Part, ctx.part_id)
    expected = {"kind": value["kind"], "request_id": value["id"], "source_message_id": human_id,
        "expected_request_revision": value["assistant"]["request_revision"],
        "options_hash": value["assistant"]["options_hash"]}
    if call.data.get("input") != expected:
        raise unavailable()
    return call


async def authorize(db, task, value, context):
    if not isinstance(context, ReplyContext):
        raise unavailable()
    ctx = context.ctx
    if (ctx.session_id != task.assistant_session_id or ctx.user_id != task.user_id
            or ctx.workspace_id != task.workspace_id):
        raise unavailable()
    main = await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
    driver = await db.get(AgentDriverState, main.id)
    if (driver is None or driver.run_id != ctx.run_id or driver.generation != ctx.run_generation
            or driver.phase not in {"running", "reserved"} or driver.abort_requested_at is not None
            or not driver.lease_expires_at or runtime.utc(driver.lease_expires_at) <= runtime.now()):
        raise unavailable()
    call = await call_evidence(db, main, ctx, value, context.source_message_id)
    if call.data.get("status") not in {"pending", "running"}:
        raise unavailable()
    inputs = list((await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == main.id,
        AgentInboxItem.user_id == ctx.user_id, AgentInboxItem.run_id == ctx.run_id,
        AgentInboxItem.generation == ctx.run_generation, AgentInboxItem.state == "claimed"))).all())
    if len(inputs) != 1 or inputs[0].message_id != context.source_message_id:
        raise unavailable()
    inbox = inputs[0]
    proof = await human_evidence(db, main, inbox)
    shown = await display_evidence(db, main, inbox, value)
    return {**proof, **shown, "call_part_id": ctx.part_id, "call_message_id": ctx.message_id,
        "run_id": ctx.run_id, "generation": ctx.run_generation}, decision(value, inbox.prompt)


def check_source(source_ref, context):
    if source_ref == {"kind": "card"} and context is None:
        return
    if (not isinstance(context, ReplyContext) or source_ref != {"kind": "human_message",
            "session_id": context.ctx.session_id, "message_id": context.source_message_id}):
        raise unavailable()


async def lock_context(db, context):
    if context is None:
        return
    # Before locking the target execution: actor -> main -> execution. Never
    # acquire the main Session from an execution-held continuation transaction.
    from agent.driver import assert_run_fence_locked
    ctx = context.ctx
    await assert_run_fence_locked(db, session_id=ctx.session_id, user_id=ctx.user_id,
        run_id=ctx.run_id, generation=ctx.run_generation)


async def source_for(db, task, kind, request_id, source_ref, context, expected):
    check_source(source_ref, context)
    if context is None:
        return {"kind": "human_card"}
    value = await get_request(db=db, user_id=task.user_id, workspace_id=task.workspace_id,
        main_id=task.assistant_session_id, kind=kind, request_id=request_id)
    proof, body = await authorize(db, task, value, context)
    if body != expected:
        raise unavailable()
    return {"kind": "human_message", "human": proof, "reply_source": source_ref, "request_kind": kind}


async def validate_saved_source(db, task, source):
    if source.get("kind") == "human_card":
        return
    if source.get("kind") != "human_message" or not isinstance(source.get("human"), dict):
        raise unavailable()
    proof = source["human"]
    main = await _authority(db, user_id=task.user_id, workspace_id=task.workspace_id, main_id=task.assistant_session_id)
    inbox = await db.get(AgentInboxItem, proof.get("inbox_id"))
    if inbox is None:
        raise unavailable()
    current = await human_evidence(db, main, inbox)
    if any(proof.get(key) != val for key, val in current.items()):
        raise unavailable()
    value = await get_request(db=db, user_id=task.user_id, workspace_id=task.workspace_id,
        main_id=task.assistant_session_id, kind=source["request_kind"], request_id=source["request_id"])
    shown = await display_evidence(db, main, inbox, value)
    if any(proof.get(key) != val for key, val in shown.items()):
        raise unavailable()
    await call_evidence(db, main, SimpleNamespace(part_id=proof.get("call_part_id"),
        message_id=proof.get("call_message_id"), run_id=proof.get("run_id"),
        run_generation=proof.get("generation")), value, inbox.message_id)
    derived = decision(value, inbox.prompt)
    if value["kind"] == "question":
        derived["attachments"] = [[] for _ in value["request"]["questions"]]
    if derived != source["decision"]:
        raise unavailable()


async def reply_from_message(*, ctx, kind, request_id, expected_request_revision, options_hash, source_message_id):
    from db.models.assistant import AssistantTask, AssistantCommand
    context = ReplyContext(ctx, source_message_id)
    source_ref = {"kind": "human_message", "session_id": ctx.session_id, "message_id": source_message_id}
    reply_id = command_digest({"domain": "request-reply", "main_id": ctx.session_id, "part_id": ctx.part_id})
    # The body is derived twice: for dispatch here, then inside decision
    # acceptance under the target Session lock. No model-supplied answer exists.
    async with get_db_session() as db:
        value = await get_request(db=db, user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            main_id=ctx.session_id, kind=kind, request_id=request_id)
        task = await db.get(AssistantTask, value["task_id"])
        prior = await db.scalar(select(AssistantCommand).where(AssistantCommand.actor_user_id == ctx.user_id,
            AssistantCommand.workspace_id == ctx.workspace_id, AssistantCommand.assistant_session_id == ctx.session_id,
            AssistantCommand.idempotency_key == reply_id))
        if prior is not None:
            if (prior.action != "request_reply" or prior.target_type != kind or prior.target_id != request_id
                    or prior.source_ref.get("reply_source") != source_ref
                    or prior.receipt.get("request_revision") != expected_request_revision
                    or prior.receipt.get("options_hash") != options_hash):
                raise AssistantError(409, "ASSISTANT_REPLY_CONFLICT", "The persisted call already made a different decision")
            await validate_saved_source(db, task, prior.source_ref)
            return dict(prior.receipt)
        _, body = await authorize(db, task, value, context)
    binding = {"reply_id": reply_id,
        "expected_request_revision": expected_request_revision, "options_hash": options_hash,
        "source_ref": source_ref, "message_context": context}
    if kind == "permission":
        from permission.permission import reply
        return await reply(request_id, user_id=ctx.user_id, **body, **binding)
    from question.question import reply
    return await reply(request_id, body["answers"], ctx.user_id, **binding)
