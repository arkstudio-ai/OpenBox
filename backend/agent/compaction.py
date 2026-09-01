"""Context compaction: overflow detection, compression, and pruning."""
from bus import bus
from bus.events import SESSION_COMPACTION_START, SESSION_COMPACTION_COMPLETE
from models.message import TokenUsage
from core.log import create_logger
from core.token import token_estimate

log = create_logger("compaction")

COMPACTION_BUFFER = 20_000  # tokens
PRUNE_MINIMUM = 20_000  # tokens
PRUNE_PROTECT = 40_000  # tokens
PRUNE_PROTECTED_TOOLS = ["skill"]

COMPACTION_PROMPT = """Provide a detailed prompt for continuing our conversation above.
Focus on information that would be helpful for continuing the conversation, including what we did, what we're doing, which files we're working on, and what we're going to do next.
The summary that you construct will be used so that another agent can read it and continue the work.

When constructing the summary, try to stick to this template:
---
## Goal

[What goal(s) is the user trying to accomplish?]

## Instructions

- [What important instructions did the user give you that are relevant]
- [If there is a plan or spec, include information about it so next agent can continue using it]

## Discoveries

[What notable things were learned during this conversation that would be useful for the next agent to know when continuing the work]

## Accomplished

[What work has been completed, what work is still in progress, and what work is left?]

## Relevant files / directories

[Construct a structured list of relevant files that have been read, edited, or created that pertain to the task at hand.]
---"""


def get_model_context_limit(model_id: str) -> int:
    """Get the context window limit for a model.

    Priority:
    1. Config models[].context_limit (user override in openbox.json)
    2. Model-family heuristic (built-in defaults)
    """
    # 1. Check config for per-model override
    try:
        from core.config import get_config
        config = get_config()
        for m in config.models:
            if m.id == model_id and m.context_limit:
                return m.context_limit
    except Exception:
        pass

    # 2. Model-family heuristic
    return _heuristic_context_limit(model_id)


#: Ordered (substrings, limit) rules — first match wins, so the narrower id
#: must come first: "gpt-5.4-mini" also contains "gpt-5.4", and matching the
#: wrong one would hand a 400k model a 1M budget and overflow every long run.
_CONTEXT_RULES: tuple[tuple[tuple[str, ...], int], ...] = (
    # GPT-5 is quoted with a much larger window direct from OpenAI, but the
    # gateways people actually route through — Codex-backed ones in
    # particular — cap it at 256k, and a deployment here was measured at
    # exactly that. Guessing high is the failure that hurts, so the default
    # matches the smaller real-world number and config raises it.
    (("gpt-5",), 256_000),
    (("gpt-4o", "o1", "o3"), 128_000),
    # Claude: the 4.5-era Haiku is the one that stayed at 200k.
    (("haiku-4-5", "haiku-4.5", "haiku-3", "claude-3"), 200_000),
    (("opus-5", "sonnet-5", "fable-5", "opus-4-8", "opus-4.8", "opus-4-7", "opus-4.7",
      "opus-4-6", "opus-4.6", "sonnet-4-6", "sonnet-4.6"), 1_000_000),
    (("claude",), 200_000),
    # DeepSeek V4 raised the whole line to 1M; the retiring legacy ids did not.
    (("deepseek-v4", "deepseek-v5"), 1_000_000),
    (("deepseek",), 128_000),
    (("gemini",), 1_000_000),
)


def _heuristic_context_limit(model_id: str) -> int:
    """Best guess at a context window from the model id alone.

    Only reached for models the config does not describe — a subagent override,
    or a fresh install running off the example config. Guessing too high is the
    dangerous direction: compaction never fires and the provider rejects the
    request outright, so unknown models get the conservative 200k.
    """
    lowered = model_id.lower()
    for names, limit in _CONTEXT_RULES:
        if any(name in lowered for name in names):
            return limit
    return 200_000


async def is_overflow(tokens: TokenUsage | None, model_id: str = "") -> bool:
    """Check if the current token usage exceeds the model's context limit.

    Uses model_id to determine the context window size. Falls back to 200K.
    Respects config.compaction.auto and config.compaction.reserved.
    """
    if not tokens:
        return False

    # Check config
    try:
        from core.config import get_config
        config = get_config()
        if config.compaction and config.compaction.auto is False:
            return False
    except Exception:
        config = None

    context_limit = get_model_context_limit(model_id) if model_id else 200_000
    count = tokens.total or (tokens.input + tokens.output + tokens.cache)

    # Use config.compaction.reserved if set, otherwise default
    reserved = COMPACTION_BUFFER
    if config and hasattr(config, "compaction") and config.compaction:
        cfg_reserved = getattr(config.compaction, "reserved", None)
        if cfg_reserved is not None:
            reserved = cfg_reserved
    reserved = min(reserved, 32000)

    usable = context_limit - reserved
    return count >= usable


async def create_compaction(session_id: str, auto: bool = True, user_id: str = "default",
                            messages: list | None = None, model_id: str = "",
                            run_fence: tuple[str, str, int] | None = None,
                            bind_trigger: bool = False):
    """Create a compaction request (special user message with compaction part).

    When `messages` is supplied, a tail of recent history is marked to survive
    verbatim — see agent/compaction_select. Without it the summary replaces
    everything, which is the older, lossier behaviour.
    """
    from session.session import create_user_message
    from models.message import CompactionPart
    from core.identifier import ascending

    log.info(f"Creating compaction request for session {session_id} auto={auto}")
    start_payload = {"userId": user_id, "sessionId": session_id}
    if run_fence is not None:
        start_payload["generation"] = run_fence[2]
    bus.publish(SESSION_COMPACTION_START, start_payload)

    try:
        # Decide how much recent history survives verbatim.
        tail_start_id = None
        if messages:
            try:
                from agent.compaction_select import select
                from core.config import get_config
                cfg = get_config()
                reserved = getattr(getattr(cfg, "compaction", None), "reserved", None) or COMPACTION_BUFFER
                usable = max(0, get_model_context_limit(model_id) - reserved)
                configured = getattr(getattr(cfg, "compaction", None), "preserve_recent_tokens", None)
                tail_turns = getattr(getattr(cfg, "compaction", None), "tail_turns", None)
                sel = select(messages, usable, configured, tail_turns)
                tail_start_id = sel.tail_start_id
                log.info(f"Compaction tail starts at {tail_start_id or '(none)'} "
                         f"(summarising {len(sel.head)}/{len(messages)} messages)")
            except Exception as e:
                # A failed tail calculation must not block compaction itself —
                # the summary-only path still keeps the session alive.
                log.warning(f"Could not compute compaction tail: {e}")

        # The empty synthetic message and its boundary descriptor are one
        # transaction. A takeover/crash can never strand a user message that
        # recovery cannot recognise as a compaction request.
        message_id = ascending("message")
        part = CompactionPart(
            id=ascending("part"),
            auto=auto,
            session_id=session_id,
            message_id=message_id,
            tail_start_id=tail_start_id,
        )
        msg = await create_user_message(
            session_id=session_id,
            text="",
            agent="compaction",
            user_id=user_id,
            run_fence=run_fence,
            bind_trigger=bind_trigger,
            message_id=message_id,
            additional_parts=(part,),
        )
        log.info(f"Created atomic compaction request: {msg.id}/{part.id}")
        return msg
    except Exception as e:
        from agent.driver import LeaseLostError

        if isinstance(e, LeaseLostError):
            raise
        log.error(f"Failed to create compaction: {e}", exc_info=True)
        return None


CHUNK_SUMMARY_PROMPT = (
    "Summarize the conversation above concisely. Focus on: "
    "1) What the user asked for, 2) What tools were called and their key results, "
    "3) Any errors encountered, 4) What files were modified. "
    "Be brief but preserve important details. Output ONLY the summary, nothing else."
)


async def _chunked_summarize(
    messages: list[dict],
    model_id: str,
    safe_limit: int,
    session_id: str,
    user_id: str,
) -> list[str]:
    """Split large message history into chunks, summarize each independently.

    When total context exceeds the model's limit, we can't send everything at once.
    Instead: split into N chunks that each fit, get a summary of each chunk from
    a lightweight LLM call, then return the summaries for a final combined pass.
    """
    from core.config import get_config
    config = get_config()
    # Use mcp_filter_model for chunk summaries (fast/cheap), fallback to main model
    chunk_model = config.mcp_filter_model or model_id

    # Calculate chunk sizes
    msg_tokens = [(token_estimate(str(m.get("content", ""))), m) for m in messages]
    total = sum(t for t, _ in msg_tokens)
    num_chunks = max(2, (total // safe_limit) + 1)
    target_per_chunk = total // num_chunks

    # Split messages into chunks
    chunks: list[list[dict]] = []
    current_chunk: list[dict] = []
    current_tokens = 0
    for tokens, msg in msg_tokens:
        current_chunk.append(msg)
        current_tokens += tokens
        if current_tokens >= target_per_chunk and len(chunks) < num_chunks - 1:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0
    if current_chunk:
        chunks.append(current_chunk)

    log.info(f"Chunked compaction: {len(chunks)} chunks from {len(messages)} messages ({total} tokens)")

    # Summarize each chunk independently
    from agent.llm import stream_llm
    from tool.tool import ToolContext

    summaries = []
    for i, chunk in enumerate(chunks):
        chunk_msgs = list(chunk)
        chunk_msgs.append({"role": "user", "content": CHUNK_SUMMARY_PROMPT})

        summary_text = ""
        try:
            ctx = ToolContext(session_id=session_id)
            async for event in stream_llm(
                agent_def=None,
                system=[],
                messages=chunk_msgs,
                tools={},
                model_id=chunk_model,
                ctx=ctx,
            ):
                if event["type"] == "text_delta":
                    summary_text += event.get("text", "")
        except Exception as e:
            log.warning(f"Chunk {i+1}/{len(chunks)} summarization failed: {e}")
            # Fallback: just take first/last few messages as text
            fallback = []
            for m in chunk[:2] + chunk[-2:]:
                c = m.get("content", "")
                if isinstance(c, str) and c:
                    fallback.append(c[:500])
            summary_text = f"[Chunk {i+1} summary failed, partial content:]\n" + "\n".join(fallback)

        summaries.append(f"## Part {i+1}/{len(chunks)}\n{summary_text}")
        log.info(f"Chunk {i+1}/{len(chunks)} summarized: {len(summary_text)} chars")

    return summaries


async def process_compaction(
    session_id: str,
    messages: list,
    model_id: str,
    auto: bool = True,
    user_id: str = "default",
    run_fence: tuple[str, str, int] | None = None,
) -> str:
    """Execute compaction: summarize conversation with LLM.

    Uses _to_llm_messages (from loop.py) to build proper messages that include
    tool calls and results — not just text — so the summary is comprehensive.

    Returns "continue" or "stop".
    """
    from agent.llm import stream_llm
    from session.session import create_assistant_message, update_message_info, save_part, create_user_message
    from models.message import TextPart
    from tool.tool import ToolContext
    from core.identifier import ascending
    from bus.events import MESSAGE_TEXT_DELTA

    # Find the compaction user message (the one with the compaction part).
    # parent_id MUST point to this message for filter_compacted() boundary detection.
    compaction_user_id = ""
    tail_start_id = None
    for msg in reversed(messages):
        role = msg.role if isinstance(msg.role, str) else msg.role.value
        if role == "user":
            for part in (msg.parts or []):
                p = part if isinstance(part, dict) else (
                    part.model_dump() if hasattr(part, "model_dump") else {})
                if p.get("type") == "compaction":
                    compaction_user_id = msg.id
                    tail_start_id = p.get("tail_start_id")
                    break
            if compaction_user_id:
                break
    if not compaction_user_id:
        compaction_user_id = messages[-1].id if messages else ""

    assistant = await create_assistant_message(
        session_id=session_id,
        parent_id=compaction_user_id,
        model_id=model_id,
        agent="compaction",
        user_id=user_id,
        run_fence=run_fence,
    )

    # Freeze the original immutable source before deriving a compact provider
    # view. Provider failure or CAS drift must never permanently prune the live
    # transcript merely because a replacement was attempted.
    # projection. The requested tail may land inside a turn; the stable range
    # moves it back to the first message after the last completely closed turn.
    # No Session lock survives this call into the provider.
    from session.event_range import (
        StableEventRangeError,
        freeze_compaction_event_range,
    )
    try:
        compaction_range = await freeze_compaction_event_range(
            session_id,
            user_id=user_id,
            compaction_user_id=compaction_user_id,
            requested_tail_start_id=tail_start_id,
            run_fence=run_fence,
        )
    except StableEventRangeError as exc:
        log.warning(f"Compaction has no stable source range: {exc}")
        assistant.summary = True
        assistant.error = {"message": f"Compaction source range is unstable: {exc}"}
        await update_message_info(
            assistant,
            user_id=user_id,
            run_fence=run_fence,
        )
        complete_payload = {"userId": user_id, "sessionId": session_id}
        if run_fence is not None:
            complete_payload["generation"] = run_fence[2]
        bus.publish(SESSION_COMPACTION_COMPLETE, complete_payload)
        return "stop"
    messages = compaction_range.source.messages()
    tail_start_id = compaction_range.tail_start_id
    messages = prune_tool_outputs_view(messages, aggressive=True)
    log.info(
        f"Frozen compaction Event range "
        f"{compaction_range.source.start_sequence}..{compaction_range.source.end_sequence} "
        f"covering {len(messages)} messages; tail={tail_start_id or '(none)'}"
    )

    # Build messages using the full LLM message builder (includes tool calls/results)
    from agent.loop import _to_llm_messages
    compaction_messages = _to_llm_messages(messages)

    # Estimate total tokens
    import json as _json
    estimated_tokens = token_estimate(
        _json.dumps(compaction_messages, ensure_ascii=False, sort_keys=True, default=str)
    )
    context_limit = get_model_context_limit(model_id)
    # Safe limit = context - compaction prompt (~300 tokens) - output buffer (~4K)
    # Single compaction can handle up to this amount; beyond it we chunk.
    safe_limit = context_limit - 4_300

    # If messages exceed the safe limit, use CHUNKED compaction:
    # Split into chunks that each fit within the context, summarize each chunk
    # independently, then combine summaries for a final pass.
    if estimated_tokens > safe_limit and len(compaction_messages) > 4:
        log.info(f"Chunked compaction: {estimated_tokens} tokens > {safe_limit} safe limit, splitting into chunks")
        chunk_summaries = await _chunked_summarize(
            compaction_messages, model_id, safe_limit, session_id, user_id,
        )
        # Replace messages with the combined chunk summaries
        compaction_messages = [
            {"role": "user", "content": "Here are summaries of the conversation so far:\n\n" + "\n\n---\n\n".join(chunk_summaries)},
        ]

    # Price the exact detached source payload the final provider call receives
    # (excluding the compaction instruction itself). Chunking, when used, has
    # already replaced the original list at this point.
    source_token_count = token_estimate(
        _json.dumps(compaction_messages, ensure_ascii=False, sort_keys=True, default=str)
    )
    # Append the compaction prompt as the final user message
    compaction_messages.append({"role": "user", "content": COMPACTION_PROMPT})

    # Create text part upfront for streaming (matching opencode's processor pattern)
    text_part_id = ascending("part")
    text_part = TextPart(
        id=text_part_id,
        text="",
        session_id=session_id,
        message_id=assistant.id,
    )
    await save_part(
        text_part,
        is_new=True,
        user_id=user_id,
        run_fence=run_fence,
    )

    # Call LLM with no tools
    ctx = ToolContext(
        session_id=session_id,
        run_id=run_fence[1] if run_fence else "",
        run_generation=run_fence[2] if run_fence else 0,
        user_id=user_id,
    )
    summary_text = ""
    stream_usage: dict = {}
    llm_error = False

    try:
        async for event in stream_llm(
            agent_def=None,
            system=[],
            messages=compaction_messages,
            tools={},
            model_id=model_id,
            ctx=ctx,
        ):
            if event["type"] == "text_delta":
                summary_text += event["text"]
                # Stream to frontend in real-time
                delta_payload = {
                    "userId": user_id,
                    "sessionId": session_id,
                    "messageId": assistant.id,
                    "partId": text_part_id,
                    "text": event["text"],
                }
                if run_fence is not None:
                    delta_payload["generation"] = run_fence[2]
                bus.publish(MESSAGE_TEXT_DELTA, delta_payload)
            elif event["type"] == "finish":
                stream_usage = event.get("usage", {})
            elif event["type"] == "error":
                log.error(f"Compaction LLM error: {event['error']}")
                llm_error = True
                break
    except Exception as e:
        log.error(f"Compaction stream error for session {session_id}: {e}")
        llm_error = True

    if llm_error or not summary_text:
        # Partial provider output is useful diagnostics, but without the CAS
        # replacement event it is never a compaction boundary.
        text_part.text = summary_text or ""
        await save_part(text_part, user_id=user_id, run_fence=run_fence)
        # Don't create a compaction boundary on failure (matching opencode:
        # filterCompacted requires both summary=True AND finish to be set).
        # Leave finish unset so this doesn't become a boundary.
        log.warning(f"Compaction failed for session {session_id}, not creating boundary")
        assistant.summary = True  # Mark as summary attempt
        # assistant.finish deliberately NOT set — prevents bad boundary
        assistant.error = {"message": "Compaction failed to produce a summary"}
        await update_message_info(
            assistant,
            user_id=user_id,
            run_fence=run_fence,
        )
        complete_payload = {"userId": user_id, "sessionId": session_id}
        if run_fence is not None:
            complete_payload["generation"] = run_fence[2]
        bus.publish(SESSION_COMPACTION_COMPLETE, complete_payload)
        return "stop"

    # The provider ran without a DB lock. Reacquire the exact owner/fence and
    # CAS the frozen range before committing the text, summary boundary,
    # CompactionPart descriptor, and immutable replacement event together.
    from session.event_range import (
        StableEventRangeDriftError,
        SummaryNotCompactError,
        finalize_compaction_replacement,
    )
    summary_token_count = token_estimate(summary_text)
    try:
        await finalize_compaction_replacement(
            frozen=compaction_range.source,
            user_id=user_id,
            compaction_user_id=compaction_user_id,
            assistant_message_id=assistant.id,
            text_part_id=text_part_id,
            summary_text=summary_text,
            tail_start_id=tail_start_id,
            source_token_count=source_token_count,
            summary_token_count=summary_token_count,
            model_id=model_id,
            usage=stream_usage,
            run_fence=run_fence,
        )
    except (StableEventRangeDriftError, SummaryNotCompactError) as exc:
        log.warning(f"Compaction replacement rejected: {exc}")
        text_part.text = summary_text
        await save_part(text_part, user_id=user_id, run_fence=run_fence)
        assistant.summary = True
        assistant.error = {"message": str(exc)}
        # finish deliberately remains unset: filter_compacted cannot mistake
        # this diagnostic attempt for a committed replacement.
        await update_message_info(
            assistant,
            user_id=user_id,
            run_fence=run_fence,
        )
        complete_payload = {"userId": user_id, "sessionId": session_id}
        if run_fence is not None:
            complete_payload["generation"] = run_fence[2]
        bus.publish(SESSION_COMPACTION_COMPLETE, complete_payload)
        return "stop"

    # The atomic helper bypasses the convenience writers, so publish the same
    # compatible finish notification after its transaction commits. Text was
    # already streamed as deltas; reconnects read the committed Part.
    from bus.events import MESSAGE_UPDATED
    message_payload = {
        "userId": user_id,
        "sessionId": session_id,
        "message": {
            "id": assistant.id,
            "role": "assistant",
            "finish": "stop",
            "summary": True,
            "model": model_id,
        },
    }
    if stream_usage:
        message_payload["message"]["tokens"] = {
            "input": stream_usage.get("input", 0),
            "output": stream_usage.get("output", 0),
            "cache": stream_usage.get("cache", 0),
            "total": stream_usage.get("total", 0),
        }
    if run_fence is not None:
        message_payload["generation"] = run_fence[2]
    bus.publish(MESSAGE_UPDATED, message_payload)

    # Reset session token_usage after compaction.
    # The cumulative totals are reset to zero so the progress bar reflects
    # post-compaction state. context = compaction output's input tokens
    # (the actual context size the LLM will see next).
    from session.session import update_session, get_session
    session = await get_session(session_id, user_id=user_id)
    context_limit = get_model_context_limit(session.model if session else "") if session else 200_000
    compaction_tokens = TokenUsage(
        input=0,
        output=0,
        cache=0,
        total=0,
        limit=context_limit,
        context=stream_usage.get("total", 0) or (stream_usage.get("input", 0) + stream_usage.get("output", 0)),
    )
    await update_session(
        session_id,
        token_usage=compaction_tokens,
        user_id=user_id,
        run_fence=run_fence,
    )

    # Broadcast updated token_usage so frontend refreshes
    from bus.events import SESSION_UPDATED
    updated_payload = {
        "userId": user_id,
        "sessionId": session_id,
        "token_usage": compaction_tokens.model_dump(),
    }
    if run_fence is not None:
        updated_payload["generation"] = run_fence[2]
    bus.publish(SESSION_UPDATED, updated_payload)

    complete_payload = {"userId": user_id, "sessionId": session_id}
    if run_fence is not None:
        complete_payload["generation"] = run_fence[2]
    bus.publish(SESSION_COMPACTION_COMPLETE, complete_payload)

    # F10: Toast notification
    try:
        from bus.bus import publish_toast
        publish_toast(user_id, "info", "Context compacted — conversation summarized to free up space")
    except Exception:
        pass

    # Auto-triggered: inject synthetic "Continue" message and keep the loop going
    if auto:
        await create_user_message(
            session_id=session_id,
            text="Context was compacted. Continue working on the current task.",
            synthetic=True,
            user_id=user_id,
            run_fence=run_fence,
        )
        return "continue"

    # Manual trigger: stop so the user can review the summary
    return "stop"


def _prune_enabled() -> bool:
    try:
        from core.config import get_config

        config = get_config()
        return config.compaction.prune is not False
    except Exception:
        return True


def _select_prunable_tool_outputs(
    msgs: list,
    *,
    aggressive: bool,
    protect_from_id: str | None,
) -> tuple[list[tuple[str, str, dict]], int]:
    total = 0
    pruned = 0
    to_prune: list[tuple[str, str, dict]] = []
    turns = 0
    in_tail = protect_from_id is not None and any(
        message.id == protect_from_id for message in msgs
    )
    for msg in reversed(msgs):
        if in_tail:
            if msg.id == protect_from_id:
                in_tail = False
            continue
        role = msg.role if isinstance(msg.role, str) else msg.role.value
        if role == "user":
            turns += 1
        if turns < 2:
            continue
        if role == "assistant" and msg.summary and getattr(msg, "agent", None) == "compaction":
            break
        for part in msg.parts or []:
            value = part if isinstance(part, dict) else (
                part.model_dump() if hasattr(part, "model_dump") else {}
            )
            if not isinstance(value, dict):
                continue
            status = getattr(value.get("status"), "value", value.get("status"))
            if value.get("type") != "tool" or status != "completed":
                continue
            if value.get("tool", "") in PRUNE_PROTECTED_TOOLS:
                continue
            state = value.get("state", {})
            if isinstance(state, dict):
                time_info = state.get("time", {})
                if isinstance(time_info, dict) and time_info.get("compacted"):
                    break
            estimate = token_estimate(value.get("output", "") or "")
            total += estimate
            protect_limit = 10_000 if aggressive else PRUNE_PROTECT
            if total > protect_limit:
                pruned += estimate
                to_prune.append((
                    value.get("message_id", "") or msg.id,
                    value.get("id", ""),
                    value,
                ))
    return to_prune, pruned


def _mark_tool_outputs_compacted(
    selected: list[tuple[str, str, dict]],
    *,
    compacted_at: float,
) -> None:
    for _, _, part_data in selected:
        if not isinstance(part_data.get("state"), dict):
            part_data["state"] = {}
        if not isinstance(part_data["state"].get("time"), dict):
            part_data["state"]["time"] = {}
        part_data["state"]["time"]["compacted"] = compacted_at


def prune_tool_outputs_view(
    messages: list,
    *,
    aggressive: bool = True,
    protect_from_id: str | None = None,
) -> list:
    """Return a detached, model-only pruned view without persistence/events."""
    from copy import deepcopy
    import time as time_mod

    detached = deepcopy(messages)
    # Pydantic ``model_dump()`` returns a new dict. Normalize detached Parts to
    # dicts first so the compacted marker selected below is applied to the
    # provider view itself, never merely to a throwaway serialization.
    for message in detached:
        message.parts = [
            part if isinstance(part, dict) else (
                part.model_dump() if hasattr(part, "model_dump") else deepcopy(part)
            )
            for part in message.parts or []
        ]
    if not _prune_enabled():
        return detached
    selected, pruned = _select_prunable_tool_outputs(
        detached,
        aggressive=aggressive,
        protect_from_id=protect_from_id,
    )
    if pruned > (0 if aggressive else PRUNE_MINIMUM):
        _mark_tool_outputs_compacted(
            selected,
            compacted_at=time_mod.time() * 1000,
        )
    return detached


async def prune_tool_outputs(session_id: str, user_id: str | None = None, aggressive: bool = False,
                             protect_from_id: str | None = None,
                             run_fence: tuple[str, str, int] | None = None) -> None:
    """Prune old tool outputs to reduce token usage.

    Scans from newest to oldest, protects the most recent PRUNE_PROTECT tokens
    of tool output, and marks older outputs as compacted. Stops at compaction
    boundaries. Respects config.compaction.prune setting.

    Args:
        aggressive: If True, protect only 10K tokens (instead of 40K) and skip
                    minimum threshold check. Used before compaction to maximize
                    token savings.
        protect_from_id: Message id at which a preserved compaction tail begins.
                    Everything from there on is left alone. Those messages are
                    replayed verbatim after the compaction and are never sent to
                    the summarizer, so erasing their output would only recreate
                    the problem the tail exists to solve — an agent that has to
                    re-read the file it just read.
    """
    if not _prune_enabled():
        return

    from session.agent_event_log import load_canonical_model_surface

    # Cleanup must inspect the same canonical transcript authority as the
    # provider loop. SQL Parts are the mutation target, never a context
    # fallback if the event prefix is missing or corrupt.
    surface = await load_canonical_model_surface(
        session_id,
        user_id=user_id or "default",
        run_fence=run_fence,
        repair_tail=False,
    )
    msgs = list(surface.messages)

    to_prune, pruned = _select_prunable_tool_outputs(
        msgs,
        aggressive=aggressive,
        protect_from_id=protect_from_id,
    )

    # Only persist if enough to prune (skip threshold in aggressive mode)
    if pruned > (0 if aggressive else PRUNE_MINIMUM):
        log.info(f"Pruning {len(to_prune)} tool outputs ({pruned} estimated tokens)")
        import time as time_mod
        compacted_ts = time_mod.time() * 1000

        from session.session import update_part_data

        _mark_tool_outputs_compacted(to_prune, compacted_at=compacted_ts)

        for msg_id, part_id, part_data in to_prune:
            # Persist to database
            if part_id and msg_id:
                try:
                    if run_fence is None:
                        await update_part_data(part_id, part_data)
                    else:
                        await update_part_data(
                            part_id,
                            part_data,
                            user_id=user_id or "default",
                            run_fence=run_fence,
                        )
                except Exception as e:
                    from agent.driver import LeaseLostError

                    if isinstance(e, LeaseLostError):
                        raise
                    log.warning(f"Failed to persist pruned part {part_id}: {e}")
