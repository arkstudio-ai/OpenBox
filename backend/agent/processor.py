"""Single step of the agent loop: one LLM turn and the tool calls it produces.

Split out of `loop.py` along the same seam opencode draws between
`session/prompt.ts` (the outer loop) and `session/processor.ts` (one step), for
the same reason: the loop's job is deciding *whether to keep going*, and a
step's job is *running one turn*. Interleaving them produced a single function
with nine levels of nesting that could not be exercised without a live model,
a live sandbox and a database.

The contract is deliberately narrow. A step never decides the run's fate: it
classifies what happened and returns, and the loop applies policy — retry
budgets, compaction attempts, step ceilings. Notably a retryable LLM error
comes back as RETRY with no sleeping and no counter of its own.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from agent.agent import get_agent
from agent.compaction import create_compaction
from agent.doom_loop import DOOM_LOOP_THRESHOLD, is_repeat_of_recent
from agent.hooks import ToolHooks
from agent.llm import provider_tool_binding, stream_llm
from agent.retry import ContextOverflowError, is_context_overflow, is_retryable
from bus import bus
from bus.events import (
    MESSAGE_TEXT_DELTA, PART_DELTA, SESSION_ERROR, SESSION_STATUS,
)
from core.identifier import ascending
from core.log import create_logger
from models.message import (
    ReasoningPart, TextPart, ToolPartData, ToolStatus,
)
from session.session import get_messages, save_part, update_message_info, update_session
from tool.tool import ToolContext

log = create_logger("agent.processor")

#: Tool-call ids we persist are bounded and sanitised. OpenAI's Responses API
#: accepts at most 64 characters and only letters, digits, underscore and dash;
#: history is replayed to whichever provider is configured now, not the one
#: that produced it. Gemini's ids violate both rules — a kilobytes-long base64
#: thought signature — so an unfiltered id poisons the conversation for every
#: later provider.
MAX_CALL_ID = 64
_CALL_ID_ILLEGAL = re.compile(r"[^A-Za-z0-9_-]")

PERSISTED_TOOL_METADATA_KEYS = frozenset({
    "exit_code", "blocked", "truncated", "count", "duration",
    "batch_size", "timings", "lease",
    "child_session_id", "subagent_type", "questions", "answers",
    # Validation tools use these to stop an unchanged retry immediately while
    # still replaying the original, structured result in full to the model.
    "validation_failed", "retry_requires_changed_args", "failure_code",
})

# A browser can disappear at any point in a long model response. The first
# chunk and the final aggregate were previously the only durable copies, so a
# refresh in between could only restore the first few characters until the
# whole turn ended. Checkpoint append-only text at a bounded rate: frequent
# enough for a near-current recovery snapshot, without a database write per
# token.
STREAM_CHECKPOINT_INTERVAL = 0.5


async def _run_parallel_safe_groups(
    items: list,
    *,
    supports_parallel,
    run_one,
    stop_requested=lambda: False,
) -> list:
    """Run ordered calls with unsafe calls acting as full barriers.

    This mirrors Codex's read/write execution gate for a completed provider
    response: consecutive parallel-safe calls share a group, while each unsafe
    call waits for the preceding group and blocks the following one. Results
    stay in provider order even when completion order differs.
    """
    results: list = []
    parallel_group: list = []

    async def flush_parallel_group() -> None:
        nonlocal parallel_group
        if not parallel_group:
            return
        tasks = [asyncio.create_task(run_one(item)) for item in parallel_group]
        parallel_group = []
        try:
            results.extend(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    for item in items:
        if stop_requested():
            break
        if supports_parallel(item):
            parallel_group.append(item)
            continue
        await flush_parallel_group()
        if stop_requested():
            break
        results.append(await run_one(item))

    if not stop_requested():
        await flush_parallel_group()
    return results


def persisted_tool_metadata(metadata: dict | None) -> dict:
    """Keep UI/diagnostic tool metadata while dropping internal payloads."""
    return {
        key: value for key, value in (metadata or {}).items()
        if key in PERSISTED_TOOL_METADATA_KEYS
    }


def unchanged_validation_failure(
    completed_tool_parts: list,
    tool_name: str,
    tool_args: dict,
    *,
    window: int = 20,
) -> str | None:
    """Return the prior failure code when a model retries unchanged arguments.

    Ordinary doom-loop detection intentionally allows two retries.  A handled
    validation response is different: the previous result already contains a
    correction recipe, so executing the exact same mutation again cannot make
    progress.  The history is scoped to one user-initiated run by the outer
    loop, which means a later explicit user retry starts with a clean slate.
    """
    current_key = json.dumps(
        tool_args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    for part in reversed(completed_tool_parts[-window:]):
        metadata = (
            part.get("metadata")
            if isinstance(part, dict)
            else getattr(part, "metadata", None)
        )
        if not isinstance(metadata, dict) or not metadata.get("retry_requires_changed_args"):
            continue
        previous_tool = part.get("tool") if isinstance(part, dict) else getattr(part, "tool", "")
        if previous_tool != tool_name:
            continue
        previous_args = (
            part.get("input")
            if isinstance(part, dict)
            else getattr(part, "input", None)
        )
        previous_key = json.dumps(
            previous_args or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if previous_key == current_key:
            return str(metadata.get("failure_code") or "validation_failed")
    return None


def sanitize_call_id(raw: str) -> str:
    """A provider-portable form of a tool-call id.

    It must stay deterministic and collision-resistant, since the id is what
    pairs a call with its result.  Keeping only the first 64 characters is not
    sufficient: Gemini thought-signature ids commonly share a long prefix, so
    two different calls could otherwise be persisted under the same id.

    The trailing strip is not cosmetic. This function is itself the source of
    the separators it removes: substitution turns `/` and `+` into `_`, and the
    64-character clip can land straight on one. An id ending in `_` is accepted
    everywhere here and then rejected by the OpenAI Responses API the next time
    the conversation is opened on a GPT model — with an error that blames the
    character set instead.
    """
    value = raw or ""
    cleaned = _CALL_ID_ILLEGAL.sub("_", value).rstrip("_-")
    # Never return empty: an id of "" pairs a call with the wrong result, or
    # with nothing at all. Only reachable when the id was entirely separators.
    if not cleaned:
        return "call"

    # Preserve already-portable ids for readable traces.  Rewritten or long
    # ids get a hash suffix so sanitisation/truncation cannot merge distinct
    # provider calls.  The suffix ends in hexadecimal, which also satisfies
    # the Responses API's no-trailing-separator rule.
    if cleaned == value and len(cleaned) <= MAX_CALL_ID:
        return cleaned

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    head_budget = MAX_CALL_ID - len(digest) - 1
    head = cleaned[:head_budget].rstrip("_-") or "call"
    return f"{head}_{digest}"


def _tool_call_payload_key(event: dict) -> str:
    """Stable identity for duplicate/collision checks without logging args."""
    return json.dumps(
        {
            "tool": event.get("tool", ""),
            "args": event.get("args") or {},
            "invalid": bool(event.get("invalid", False)),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _event_stream_seq(event: Mapping[str, object], fallback: int) -> int:
    """Use the provider's ordered native sequence when one was verified."""

    value = event.get("stream_seq")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


def prepare_tool_call_batch(events: list[dict]) -> tuple[list[dict], list[int], bool]:
    """Canonicalise one assistant turn and detect provider-id conflicts.

    Returns ``(unique_events, duplicate_indexes, has_conflict)``.  Identical
    repeated events are idempotent and execute once.  Reusing one non-empty
    provider id for different payloads is a protocol violation: the entire
    batch must fail closed before any executor is entered, otherwise the
    persisted result can later be paired with the wrong call.

    Providers occasionally omit an id.  Those calls receive a deterministic
    per-position id instead of all collapsing to the generic ``call`` value.
    """
    prepared: list[dict] = []
    duplicate_indexes: list[int] = []
    seen: dict[str, tuple[str, int]] = {}
    has_conflict = False

    for index, original in enumerate(events):
        event = dict(original)
        payload_key = _tool_call_payload_key(event)
        raw_id = str(event.get("call_id") or "")
        canonical_id = sanitize_call_id(
            raw_id if raw_id else f"generated:{index}:{payload_key}"
        )
        event["_canonical_call_id"] = canonical_id
        event["_batch_index"] = index

        previous = seen.get(canonical_id)
        if previous is None:
            seen[canonical_id] = (payload_key, index)
            prepared.append(event)
            continue

        previous_payload, _previous_index = previous
        if raw_id and previous_payload != payload_key:
            has_conflict = True
        elif previous_payload == payload_key:
            duplicate_indexes.append(index)
        else:
            # A missing provider id is generated from index+payload and should
            # never collide.  Treat an unexpected collision as hostile input.
            has_conflict = True

    return prepared, duplicate_indexes, has_conflict


def _canonical_for_wire(wire_name: str, wire_to_canonical: Mapping[str, str]) -> str | None:
    """Resolve only the same case repair accepted at the provider boundary."""

    canonical = wire_to_canonical.get(wire_name)
    if canonical is not None:
        return str(canonical)
    lowered = wire_name.lower()
    if lowered != wire_name:
        canonical = wire_to_canonical.get(lowered)
        if canonical is not None:
            return str(canonical)
    return None


def _persistable_wire_name(raw_name: object) -> str:
    """Keep valid provider names exact; bind malformed names opaquely."""

    value = str(raw_name or "")
    if 0 < len(value) <= 128 and not any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value):
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()
    return f"invalid_wire_v1_{digest}"


def _tool_part_runtime_identity(
    event: Mapping[str, object],
    *,
    stream_seq: int,
    wire_to_canonical: Mapping[str, str],
    provider_binding_digest: str,
    provider_dialect: str,
) -> dict[str, object]:
    """Private persistence fields shared by every tool-call outcome path."""

    resolved_name = str(event.get("tool") or "")
    raw_wire_name = str(event.get("wire_tool") or resolved_name)
    canonical = (
        _canonical_for_wire(resolved_name, wire_to_canonical)
        or _canonical_for_wire(raw_wire_name, wire_to_canonical)
    )
    if canonical is None:
        # Unknown/blocked calls still need a complete, non-spoofable identity
        # so a partial rollout cannot later mistake their display name for an
        # authorized canonical tool. They remain outside executable_ids.
        canonical = f"invalid:v1:{hashlib.sha256(raw_wire_name.encode()).hexdigest()}"
    return {
        "canonical_tool_id": canonical,
        "wire_tool_name": _persistable_wire_name(raw_wire_name),
        "provider_binding_digest": provider_binding_digest,
        "provider_dialect": provider_dialect,
        "stream_seq": stream_seq,
    }


class StepOutcome(str, Enum):
    """What the loop should do next."""

    CONTINUE = "continue"   # turn completed; the loop re-evaluates termination
    COMPACT = "compact"     # context overflowed; compaction has been requested
    RETRY = "retry"         # transient LLM failure; the loop owns the budget
    ERROR = "error"         # unrecoverable; the run ends


@dataclass
class StepResult:
    """Everything the loop needs to know about one turn."""

    outcome: StepOutcome
    finish_reason: str = "unknown"
    text: str = ""
    reasoning: str = ""
    usage: dict = field(default_factory=lambda: {"input": 0, "output": 0, "total": 0})
    # Completed tool parts from this step, appended to the loop's doom-loop
    # history. Returned rather than mutated so the step owns no shared state.
    completed_tool_parts: list = field(default_factory=list)
    agent_switch: str | None = None
    retry_reason: str | None = None
    error: str | None = None
    duration: float = 0.0


async def _iter_until_abort(stream, abort: asyncio.Event):
    """Yield stream events until exhaustion OR abort — whichever comes first.

    The old `async for … if abort.is_set(): break` pattern only noticed an
    abort after the NEXT chunk arrived, so a model silently composing a long
    tool call ignored the stop button for the whole silence. Racing each
    __anext__ against abort.wait() reacts immediately, and closing the
    generator tears down the provider's HTTP stream — opencode's AbortSignal
    semantics (its abort cancels the in-flight request, not just a flag).
    """
    import contextlib

    if abort.is_set():
        await stream.aclose()
        return
    abort_task = asyncio.create_task(abort.wait())
    try:
        while True:
            next_task = asyncio.create_task(anext(stream))
            done, _ = await asyncio.wait({next_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
            if next_task not in done:
                next_task.cancel()
                with contextlib.suppress(BaseException):
                    await next_task
                with contextlib.suppress(BaseException):
                    await stream.aclose()
                return
            try:
                event = next_task.result()
            except StopAsyncIteration:
                return
            yield event
            if abort.is_set():
                with contextlib.suppress(BaseException):
                    await stream.aclose()
                return
    finally:
        abort_task.cancel()
        with contextlib.suppress(BaseException):
            await abort_task


async def _history_for_compaction(session_id: str, user_id: str) -> list:
    """History for sizing the preserved tail. Best effort — losing the tail is
    much better than losing the compaction that keeps the session alive."""
    try:
        return await get_messages(session_id, user_id=user_id)
    except Exception as e:
        log.warning(f"Could not load history for compaction tail: {e}")
        return []


async def process_step(
    *,
    session_id: str,
    user_id: str,
    session,
    agent_def,
    system: list[str],
    llm_messages: list[dict],
    tools: dict,
    model_id: str,
    ctx: ToolContext,
    hooks: ToolHooks,
    assistant_info,
    sandbox,
    abort,
    doom_loop_history: list,
    user_variant: str | None = None,
    tool_choice: str | None = None,
    execution_lookup: Mapping[str, object] | None = None,
    step_executable_ids: frozenset[str] | set[str] | None = None,
    provider_to_canonical: Mapping[str, str] | None = None,
    provider_binding_digest: str | None = None,
    provider_dialect: str | None = None,
) -> StepResult:
    """Run one LLM turn: stream it, persist its parts, execute its tool calls.

    `doom_loop_history` is read, never written — new parts come back on the
    result so the caller stays the only writer.
    """
    collected_text = ""
    collected_reasoning = ""
    text_part_id = None
    reasoning_part_id = None
    text_checkpoint_at = 0.0
    reasoning_checkpoint_at = 0.0
    pending_tool_calls = []      # collected during the stream, executed after
    streaming_tool_parts: dict[int, str] = {}   # tool-call index -> part id
    total_usage = {"input": 0, "output": 0, "total": 0}
    finish_reason = "unknown"
    completed_tool_parts: list = []
    agent_switch: str | None = None
    step_start_time = time.time()
    provider_event_received = False
    execution_tools = dict(execution_lookup) if execution_lookup is not None else dict(tools)
    executable_ids = (
        frozenset(step_executable_ids)
        if step_executable_ids is not None
        else frozenset(execution_tools)
    )
    response_executable = set(executable_ids)
    wire_to_canonical = (
        dict(provider_to_canonical)
        if provider_to_canonical is not None
        else {name: name for name in tools}
    )
    if (provider_binding_digest is None) != (provider_dialect is None):
        raise ValueError("provider binding digest and dialect must be supplied together")
    if provider_binding_digest is None:
        binding = provider_tool_binding(
            model_id,
            provider_to_canonical=wire_to_canonical,
        )
        provider_binding_digest = binding.digest()
        provider_dialect = binding.dialect
    assert provider_dialect is not None
    visible_wire_names = tuple(sorted(
        name
        for name, canonical_id in wire_to_canonical.items()
        if canonical_id in executable_ids and name in tools
    ))
    # Nested dispatchers must observe the same execution frontier as direct
    # calls; the complete eligible catalogue is never placed here.
    ctx.available_tools = frozenset(response_executable)

    try:
        llm_stream = stream_llm(
            agent_def=agent_def,
            system=system,
            messages=llm_messages,
            tools=tools,
            model_id=model_id,
            ctx=ctx,
            hooks=hooks,
            variant=user_variant,
            tool_choice=tool_choice,
        )
        async for event in _iter_until_abort(llm_stream, abort):
            # Once any provider event has crossed the stream boundary, this
            # response may already have visible text, persisted native state,
            # or a pending tool card. Replaying the whole request after a
            # transport error could duplicate narration or side effects.
            provider_event_received = True
            if event["type"] == "reasoning_delta":
                text = event["text"]
                collected_reasoning += text

                if not reasoning_part_id:
                    reasoning_part_id = ascending("part")
                    reasoning_part = ReasoningPart(
                        id=reasoning_part_id,
                        text=text,
                        session_id=session_id,
                        message_id=assistant_info.id,
                    )
                    await save_part(reasoning_part, is_new=True, user_id=user_id)
                    reasoning_checkpoint_at = time.monotonic()
                else:
                    from bus.events import PART_DELTA
                    bus.publish(PART_DELTA, {
                        "userId": user_id,
                        "sessionId": session_id,
                        "messageId": assistant_info.id,
                        "partId": reasoning_part_id,
                        "delta": text,
                    })
                    now = time.monotonic()
                    if now - reasoning_checkpoint_at >= STREAM_CHECKPOINT_INTERVAL:
                        await save_part(
                            ReasoningPart(
                                id=reasoning_part_id,
                                text=collected_reasoning,
                                session_id=session_id,
                                message_id=assistant_info.id,
                            ),
                            user_id=user_id,
                        )
                        reasoning_checkpoint_at = now

            elif event["type"] == "text_delta":
                text = event["text"]
                collected_text += text

                if not text_part_id:
                    text_part_id = ascending("part")
                    text_part = TextPart(
                        id=text_part_id,
                        text=text,
                        session_id=session_id,
                        message_id=assistant_info.id,
                    )
                    await save_part(text_part, is_new=True, user_id=user_id)
                    text_checkpoint_at = time.monotonic()
                else:
                    bus.publish(MESSAGE_TEXT_DELTA, {
                        "userId": user_id,
                        "sessionId": session_id,
                        "messageId": assistant_info.id,
                        "partId": text_part_id,
                        "text": text,
                    })
                    now = time.monotonic()
                    if now - text_checkpoint_at >= STREAM_CHECKPOINT_INTERVAL:
                        await save_part(
                            TextPart(
                                id=text_part_id,
                                text=collected_text,
                                session_id=session_id,
                                message_id=assistant_info.id,
                            ),
                            user_id=user_id,
                        )
                        text_checkpoint_at = now

            elif event["type"] in {"native_search_started", "native_search_result"}:
                if ctx._native_binding is None or ctx._native_capability_key is None:
                    raise RuntimeError("native provider event arrived without a binding")
                from session.internal_parts import (
                    PROVIDER_TRANSCRIPT_KIND,
                    save_internal_part,
                )

                await save_internal_part(
                    session_id=session_id,
                    user_id=user_id,
                    message_id=assistant_info.id,
                    kind=PROVIDER_TRANSCRIPT_KIND,
                    data=event["raw_item"],
                    binding=ctx._native_binding,
                    capability_key_digest=ctx._native_capability_key.digest(),
                    response_chain_id=event["response_chain_id"],
                    stream_seq=int(event["stream_seq"]),
                    idempotency_key=(
                        f"{event['response_chain_id']}:{event['stream_seq']}:"
                        f"{event['type']}"
                    ),
                )

            elif event["type"] == "native_tool_revealed":
                if ctx._native_binding is None or ctx._native_capability_key is None:
                    raise RuntimeError("native reveal arrived without a capability binding")
                catalogue = ctx._capability_catalog
                canonical_id = str(event.get("canonical_tool_id") or "")
                entry = (
                    catalogue.entries.get(canonical_id)
                    if catalogue is not None
                    else None
                )
                native_plan = ctx._native_tool_plan
                if (
                    entry is None
                    or native_plan is None
                    or str(event.get("wire_tool_name") or "")
                    not in native_plan.deferred_wire_names
                    or native_plan.wire_to_canonical.get(
                        str(event.get("wire_tool_name") or "")
                    )
                    != canonical_id
                ):
                    raise RuntimeError("native reveal is outside the eligible frontier")
                # The provider event is evidence, not authority. Same-response
                # execution is granted only by the server-owned catalogue bit;
                # reject a forged/misparsed elevation before it can persist any
                # reveal evidence.
                if event.get("same_response_executable") and not entry.same_response_safe:
                    raise RuntimeError(
                        "native reveal attempted unauthorized same-response execution"
                    )

                from session.internal_parts import (
                    PROVIDER_TRANSCRIPT_KIND,
                    ToolRevealEvent,
                    commit_tool_reveal,
                    save_internal_part,
                )

                provider_item = event["raw_item"].get("provider_item")
                if (
                    isinstance(provider_item, dict)
                    and provider_item.get("type") == "tool_reference"
                ):
                    await save_internal_part(
                        session_id=session_id,
                        user_id=user_id,
                        message_id=assistant_info.id,
                        kind=PROVIDER_TRANSCRIPT_KIND,
                        data=provider_item,
                        binding=ctx._native_binding,
                        capability_key_digest=ctx._native_capability_key.digest(),
                        response_chain_id=event["response_chain_id"],
                        stream_seq=int(event["stream_seq"]),
                        idempotency_key=(
                            f"{event['response_chain_id']}:{event['stream_seq']}:"
                            f"tool_reference:{canonical_id}"
                        ),
                    )

                origin = await save_internal_part(
                    session_id=session_id,
                    user_id=user_id,
                    message_id=assistant_info.id,
                    kind="provider_tool_reveal",
                    data=event["raw_item"],
                    binding=ctx._native_binding,
                    capability_key_digest=ctx._native_capability_key.digest(),
                    response_chain_id=event["response_chain_id"],
                    stream_seq=int(event["stream_seq"]),
                    idempotency_key=(
                        f"{event['response_chain_id']}:{event['stream_seq']}:"
                        f"tool_revealed:{canonical_id}"
                    ),
                )
                await commit_tool_reveal(
                    ToolRevealEvent(
                        session_id=session_id,
                        user_id=user_id,
                        message_id=assistant_info.id,
                        origin_part_id=origin.id,
                        agent_id=ctx.agent_id,
                        canonical_tool_id=canonical_id,
                        schema_digest=entry.schema_digest,
                        catalog_generation=catalogue.generation,
                        evidence_source="native",
                        stream_seq=int(event["stream_seq"]),
                        capability_key_digest=ctx._native_capability_key.digest(),
                        response_chain_id=event["response_chain_id"],
                    ),
                    ttl_seconds=ctx._native_reveal_ttl_seconds,
                    max_reveals=ctx._native_max_persisted_reveals,
                )
                if event.get("same_response_executable"):
                    response_executable.add(canonical_id)
                    ctx.available_tools = frozenset(response_executable)

            elif event["type"] == "tool_call_start":
                # LLM just started emitting a tool call — create a pending
                # tool part immediately so the frontend can show the card.
                tc_index = event["index"]
                tc_part_id = ascending("part")
                streaming_tool_parts[tc_index] = tc_part_id
                tool_part = ToolPartData(
                    id=tc_part_id,
                    tool=event["tool"],
                    status=ToolStatus.PENDING,
                    input={},
                    **_tool_part_runtime_identity(
                        event,
                        stream_seq=int(tc_index),
                        wire_to_canonical=wire_to_canonical,
                        provider_binding_digest=provider_binding_digest,
                        provider_dialect=provider_dialect,
                    ),
                    session_id=session_id,
                    message_id=assistant_info.id,
                )
                await save_part(tool_part, is_new=True, user_id=user_id)

            elif event["type"] == "tool_call_args_delta":
                # Stream argument chunks to the frontend for live preview.
                tc_index = event["index"]
                tc_part_id = streaming_tool_parts.get(tc_index)
                if tc_part_id:
                    from bus.events import PART_DELTA
                    bus.publish(PART_DELTA, {
                        "userId": user_id,
                        "sessionId": session_id,
                        "messageId": assistant_info.id,
                        "partId": tc_part_id,
                        "delta": event["delta"],
                    })

            elif event["type"] == "tool_call":
                pending_tool_calls.append(event)

            elif event["type"] == "finish":
                finish_reason = event.get("reason", "stop")
                total_usage = event.get("usage", {})

            elif event["type"] == "error":
                error = event["error"]
                if is_context_overflow(str(error)):
                    await create_compaction(session_id, auto=True, user_id=user_id,
                                        messages=await _history_for_compaction(session_id, user_id),
                                        model_id=model_id)
                    finish_reason = "compact"
                else:
                    raise error

        # Validate the complete batch before entering any executor.  A reused
        # provider call id with different payloads is ambiguous on replay; if
        # even one exists, fail the whole batch closed rather than allowing a
        # safe-looking prefix of calls to cause side effects.
        original_tool_calls = pending_tool_calls
        pending_tool_calls, duplicate_indexes, has_call_id_conflict = (
            prepare_tool_call_batch(original_tool_calls)
        )

        if has_call_id_conflict:
            log.warning(
                "Rejected assistant tool-call batch with conflicting call ids "
                "(count=%d)",
                len(original_tool_calls),
            )
            for tc_idx, tc_event in enumerate(original_tool_calls):
                collision_id = sanitize_call_id(
                    f"conflict:{tc_idx}:{tc_event.get('call_id') or ''}:"
                    f"{_tool_call_payload_key(tc_event)}"
                )
                existing_part_id = streaming_tool_parts.get(tc_idx)
                tool_part = ToolPartData(
                    id=existing_part_id or ascending("part"),
                    tool=tc_event.get("tool", ""),
                    status=ToolStatus.ERROR,
                    input=tc_event.get("args") or {},
                    call_id=collision_id,
                    **_tool_part_runtime_identity(
                        tc_event,
                        stream_seq=_event_stream_seq(tc_event, tc_idx),
                        wire_to_canonical=wire_to_canonical,
                        provider_binding_digest=provider_binding_digest,
                        provider_dialect=provider_dialect,
                    ),
                    error=(
                        "Provider returned conflicting tool calls with the same call id; "
                        "the entire batch was blocked before execution."
                    ),
                    title="Conflicting tool call ids blocked",
                    metadata={"blocked": True},
                    session_id=session_id,
                    message_id=assistant_info.id,
                )
                await save_part(
                    tool_part,
                    is_new=not bool(existing_part_id),
                    user_id=user_id,
                )
            pending_tool_calls = []

        # A byte-for-byte duplicate event is an idempotent transport replay,
        # not a second execution.  Usually no second streaming card exists; if
        # one does, close it explicitly so no pending part is stranded.
        if not has_call_id_conflict:
            for duplicate_index in duplicate_indexes:
                duplicate_part_id = streaming_tool_parts.get(duplicate_index)
                if not duplicate_part_id:
                    continue
                duplicate_event = original_tool_calls[duplicate_index]
                duplicate_part = ToolPartData(
                    id=duplicate_part_id,
                    tool=duplicate_event.get("tool", ""),
                    status=ToolStatus.ERROR,
                    input=duplicate_event.get("args") or {},
                    call_id=sanitize_call_id(
                        f"duplicate:{duplicate_index}:"
                        f"{duplicate_event.get('call_id') or ''}:"
                        f"{_tool_call_payload_key(duplicate_event)}"
                    ),
                    **_tool_part_runtime_identity(
                        duplicate_event,
                        stream_seq=_event_stream_seq(
                            duplicate_event,
                            duplicate_index,
                        ),
                        wire_to_canonical=wire_to_canonical,
                        provider_binding_digest=provider_binding_digest,
                        provider_dialect=provider_dialect,
                    ),
                    error="Duplicate provider tool-call event ignored; the original executes once.",
                    title="Duplicate tool call ignored",
                    metadata={"blocked": True},
                    session_id=session_id,
                    message_id=assistant_info.id,
                )
                await save_part(duplicate_part, is_new=False, user_id=user_id)

        # Execute tool calls after the stream completes. Calls explicitly
        # marked parallel-safe run together; an unsafe call is a barrier before
        # and after itself. Each call gets a shallow context copy because hooks
        # bind part_id, incremental output and authorization identity to it.
        ctx.message_id = assistant_info.id

        def supports_parallel(tc_event: dict) -> bool:
            tool_name = tc_event.get("tool", "")
            canonical_id = wire_to_canonical.get(tool_name)
            if (
                tc_event.get("invalid", False)
                or tc_event.get("native_error_code") is not None
                or canonical_id is None
                or canonical_id not in response_executable
            ):
                return False
            tool_info = execution_tools.get(str(canonical_id))
            return bool(tool_info and getattr(tool_info, "parallel_safe", False))

        async def execute_one(tc_event: dict):
            if abort.is_set():
                return None

            tc_idx = int(tc_event["_batch_index"])
            tool_name = tc_event["tool"]
            tool_args = tc_event["args"]
            canonical_tool_id = wire_to_canonical.get(tool_name)
            native_error_code = tc_event.get("native_error_code")
            is_invalid = (
                tc_event.get("invalid", False)
                or canonical_tool_id is None
                or canonical_tool_id not in response_executable
                or native_error_code is not None
            )

            # Bounded, portable and collision-resistant. The full batch was
            # validated above before any side effect was allowed.
            llm_call_id = str(tc_event["_canonical_call_id"])
            existing_part_id = streaming_tool_parts.get(tc_idx)
            tool_part = ToolPartData(
                id=existing_part_id or ascending("part"),
                tool=tool_name,
                status=ToolStatus.RUNNING,
                input=tool_args,
                call_id=llm_call_id,
                **_tool_part_runtime_identity(
                    tc_event,
                    stream_seq=_event_stream_seq(tc_event, tc_idx),
                    wire_to_canonical=wire_to_canonical,
                    provider_binding_digest=provider_binding_digest,
                    provider_dialect=provider_dialect,
                ),
                session_id=session_id,
                message_id=assistant_info.id,
            )
            await save_part(
                tool_part,
                is_new=not bool(existing_part_id),
                user_id=user_id,
            )

            if is_invalid:
                tool_part.status = ToolStatus.ERROR
                if native_error_code == "deferred_until_next_step":
                    tool_part.title = "Deferred tool available next step"
                    tool_part.error = (
                        "deferred_until_next_step: the tool was safely revealed, "
                        "but its execution policy requires a newly planned step and "
                        "a new call id. No executor was entered."
                    )
                    tool_part.metadata = {
                        "blocked": True,
                        "failure_code": "deferred_until_next_step",
                    }
                else:
                    tool_part.error = (
                        f"Tool '{tool_name}' is not materialized for this step. "
                        f"Available: {', '.join(visible_wire_names)}"
                    )
                await save_part(tool_part, user_id=user_id)
                return None

            # Repeating a handled validation error with unchanged arguments
            # cannot make progress. Stateful validation tools are barriers, so
            # completed_tool_parts is stable while this check runs.
            prior_failure = unchanged_validation_failure(
                [*doom_loop_history, *completed_tool_parts],
                tool_name,
                tool_args,
            )
            if prior_failure:
                log.warning(
                    "Blocked unchanged retry after %s: %s",
                    prior_failure,
                    tool_name,
                )
                tool_part.status = ToolStatus.ERROR
                tool_part.title = "Unchanged validation retry blocked"
                tool_part.error = (
                    f"Identical retry blocked after {prior_failure}. Change the arguments using "
                    "the previous result's corrected_prompt_template before calling this tool again."
                )
                tool_part.metadata = {
                    "blocked": True,
                    "failure_code": prior_failure,
                }
                await save_part(tool_part, user_id=user_id)
                return None

            if is_repeat_of_recent(doom_loop_history, tool_name, tool_args):
                log.warning(
                    f"Doom loop detected: {tool_name} called "
                    f"{DOOM_LOOP_THRESHOLD} times with same args"
                )
                tool_part.status = ToolStatus.ERROR
                tool_part.error = (
                    f"Doom loop detected: '{tool_name}' has been called {DOOM_LOOP_THRESHOLD} "
                    f"consecutive times with identical arguments. Breaking the loop. "
                    f"Please try a different approach."
                )
                await save_part(tool_part, user_id=user_id)
                return None

            tool_info = execution_tools.get(str(canonical_tool_id))
            if not tool_info:
                return None

            # Stateful/barrier tools keep the original context because some
            # capability commits intentionally observe their bound part_id
            # after execution. Parallel-safe calls must be isolated from one
            # another because hooks mutate per-call fields on the context.
            call_ctx = copy.copy(ctx) if supports_parallel(tc_event) else ctx
            call_ctx.message_id = assistant_info.id
            exec_task = asyncio.create_task(
                hooks.wrap_execute(
                    str(canonical_tool_id),
                    tool_info.execute,
                    tool_args,
                    call_ctx,
                    part_id=tool_part.id,
                )
            )
            abort_task = asyncio.create_task(abort.wait())
            done, _ = await asyncio.wait(
                {exec_task, abort_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            abort_task.cancel()
            if exec_task not in done:
                exec_task.cancel()
                try:
                    await exec_task
                except (asyncio.CancelledError, Exception):
                    pass
                return None
            result = exec_task.result()

            tool_part.status = (
                ToolStatus.COMPLETED
                if not result.metadata.get("error")
                else ToolStatus.ERROR
            )
            tool_part.output = result.output
            tool_part.title = result.title
            tool_part.error = result.output if result.metadata.get("error") else None
            tool_part.metadata = persisted_tool_metadata(result.metadata)
            await save_part(tool_part, user_id=user_id)
            return tool_part, result

        tool_outcomes = await _run_parallel_safe_groups(
            pending_tool_calls,
            supports_parallel=supports_parallel,
            run_one=execute_one,
            stop_requested=abort.is_set,
        )

        # Gather preserves provider order, so shared loop state stays
        # deterministic even when parallel tools finish out of order.
        for outcome in tool_outcomes:
            if outcome is None:
                continue
            tool_part, result = outcome
            agent_switch = result.metadata.get("agent_switch")
            if agent_switch:
                try:
                    get_agent(agent_switch)
                    await update_session(
                        session_id,
                        user_id=user_id,
                        agent=agent_switch,
                    )
                    log.info(f"Agent switched to {agent_switch}")
                except ValueError:
                    log.warning(f"Unknown agent for switch: {agent_switch}")

            completed_tool_parts.append(tool_part)
            if result.metadata.get("plan_ready"):
                finish_reason = "stop"

    except ContextOverflowError:
        await create_compaction(session_id, auto=True, user_id=user_id,
                                        messages=await _history_for_compaction(session_id, user_id),
                                        model_id=model_id)
        finish_reason = "compact"
    except Exception as e:
        # Preserve partial prose as process narration before returning early;
        # the normal final-save block below is skipped by both retry and error
        # outcomes, and leaving channel unset makes a legacy client guess.
        if text_part_id and collected_text:
            try:
                await save_part(
                    TextPart(
                        id=text_part_id,
                        text=collected_text,
                        channel="commentary",
                        session_id=session_id,
                        message_id=assistant_info.id,
                    ),
                    user_id=user_id,
                )
            except Exception:
                log.warning("Could not checkpoint partial text after LLM failure", exc_info=True)
        retry_msg = is_retryable(e)
        if retry_msg and not provider_event_received:
            # Classify only. Whether to retry at all, and how long to wait, is
            # retry *policy* — it belongs to the caller that owns the attempt
            # counter, not to a single step. A partially consumed response is
            # never replay-safe, even when its transport error is transient.
            return StepResult(
                outcome=StepOutcome.RETRY,
                retry_reason=retry_msg,
                error=str(e),
                duration=time.time() - step_start_time,
            )
        log.error(f"LLM error in session {session_id}: {e}")
        bus.publish(SESSION_ERROR, {
            "userId": user_id,
            "sessionId": session_id,
            "error": {"message": str(e)},
        })
        assistant_info.error = {"message": str(e)}
        await update_message_info(assistant_info, user_id=user_id)
        return StepResult(
            outcome=StepOutcome.ERROR,
            error=str(e),
            duration=time.time() - step_start_time,
        )

    # Save final reasoning part (full text)
    if reasoning_part_id and collected_reasoning:
        final_reasoning = ReasoningPart(
            id=reasoning_part_id,
            text=collected_reasoning,
            session_id=session_id,
            message_id=assistant_info.id,
        )
        await save_part(final_reasoning, user_id=user_id)

    # Save final text part (full text)
    if text_part_id and collected_text:
        final_text = TextPart(
            id=text_part_id,
            text=collected_text,
            # A step that hands control to tools is narration, not the answer.
            # Persisting that distinction prevents a stopped/interrupted run
            # from presenting "I will continue..." as its final response.
            channel="final" if finish_reason == "stop" else "commentary",
            session_id=session_id,
            message_id=assistant_info.id,
        )
        await save_part(final_text, user_id=user_id)

    return StepResult(
        outcome=StepOutcome.COMPACT if finish_reason == "compact" else StepOutcome.CONTINUE,
        finish_reason=finish_reason,
        text=collected_text,
        reasoning=collected_reasoning,
        usage=total_usage,
        completed_tool_parts=completed_tool_parts,
        agent_switch=agent_switch,
        duration=time.time() - step_start_time,
    )
