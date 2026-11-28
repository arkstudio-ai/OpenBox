"""Call-site facade for the registered turn observer and call wrapper.

Kernel code records its facts here, at the places it always did, with the same
payloads; ``agent.extensions`` decides who receives them. With no plugin
installed every function is a no-op and identities are None.
"""
from __future__ import annotations

from typing import Any

from agent.extensions import extensions


# --- Turn observer: facts ---------------------------------------------------

async def record(type: str, data: dict, *, context: Any = None, db: Any = None,
                 event_id: str | None = None, occurred_at: Any = None) -> None:
    await extensions().recorder.record(type, data, context=context, db=db, event_id=event_id,
                                       occurred_at=occurred_at)


def record_stream(context, event: dict):
    return extensions().recorder.record_stream(context, event)


def emit(type: str, data: dict, *, context: Any = None, event_id: str | None = None):
    return extensions().recorder.emit(type, data, context=context, event_id=event_id)


def emit_control(control: dict, *, db: Any = None):
    return extensions().recorder.emit_control(control, db=db)


def timestamp(value) -> str | None:
    return extensions().recorder.timestamp(value)


# --- Turn observer: identity -------------------------------------------------

def enabled(user_id: str | None = None) -> bool:
    return extensions().recorder.enabled(user_id)


def current():
    return extensions().recorder.current()


def bind(context):
    return extensions().recorder.bind(context)


def new_identity(user_id: str, session_id: str, **fields):
    return extensions().recorder.new_identity(user_id, session_id, **fields)


def parse_identity(saved):
    return extensions().recorder.parse_identity(saved)


def saved_identity(saved) -> dict:
    return extensions().recorder.saved_identity(saved)


def markers(saved) -> dict:
    return extensions().recorder.markers(saved)


async def session_identity(db, user_id: str, session_id: str, **ids):
    return await extensions().recorder.session_identity(db, user_id, session_id, **ids)


async def activity_identity(db, user_id: str, session_id: str, *, saved: dict | None = None, **ids):
    return await extensions().recorder.activity_identity(db, user_id, session_id, saved=saved, **ids)


async def mark_recording_in_tx(db, execution, *, user_id: str, session_id: str, context) -> None:
    await extensions().recorder.mark_recording_in_tx(db, execution, user_id=user_id,
                                                     session_id=session_id, context=context)


async def paused_in_tx(db, context) -> bool:
    return await extensions().recorder.paused_in_tx(db, context)


async def baseline_candidate_in_tx(db, *, user_id: str, session_id: str, context, pause: bool = False):
    return await extensions().recorder.baseline_candidate_in_tx(db, user_id=user_id, session_id=session_id,
                                                                context=context, pause=pause)


async def capture_asset_ids_in_tx(db, context, asset_ids, *, role: str) -> dict:
    return await extensions().recorder.capture_asset_ids_in_tx(db, context, asset_ids, role=role)


def context_of(ctx):
    return extensions().recorder.context_of(ctx)


def attach(ctx, context) -> None:
    extensions().recorder.attach(ctx, context)


async def context_for_tool(ctx):
    return await extensions().recorder.context_for_tool(ctx)


def public_value(value: Any) -> Any:
    return extensions().recorder.public_value(value)


def tool_schema(tool_info, name: str) -> dict:
    return extensions().recorder.tool_schema(tool_info, name)


def requested_tool_schema(ctx, name: str, tool_info=None) -> tuple[dict, str]:
    return extensions().recorder.requested_tool_schema(ctx, name, tool_info)


# --- Call wrapper ------------------------------------------------------------

async def begin_model_call(ctx, *, purpose: str, model_id: str, payload, capture_level: str):
    return await extensions().call_wrapper.begin_model_call(
        ctx, purpose=purpose, model_id=model_id, payload=payload, capture_level=capture_level)


def chunk_blocks(kind: str):
    return extensions().call_wrapper.chunk_blocks(kind)


async def capture_billing(context, meter, usage, credits, purpose: str) -> None:
    await extensions().call_wrapper.capture_billing(context, meter, usage, credits, purpose)


def bind_model_billing(ctx, meter) -> None:
    extensions().call_wrapper.bind_model_billing(ctx, meter)


def active_request(ctx):
    return extensions().call_wrapper.active_request(ctx)


def note_model_attempt(ctx, attempt: int) -> None:
    extensions().call_wrapper.note_model_attempt(ctx, attempt)


def media_capture(ctx, enabled: bool):
    return extensions().call_wrapper.media_capture(ctx, enabled)


def note_tool_request(ctx, tool_info, provider_call_id, arguments_raw) -> None:
    extensions().call_wrapper.note_tool_request(ctx, tool_info, provider_call_id, arguments_raw)


def pending_tool_request(ctx) -> tuple:
    return extensions().call_wrapper.pending_tool_request(ctx)


def begin_tool_body(ctx, context, *, tool: str, owner, stopped=None):
    return extensions().call_wrapper.begin_tool_body(ctx, context, tool=tool, owner=owner, stopped=stopped)


def tool_body(ctx, context, *, tool: str, owner):
    return extensions().call_wrapper.tool_body(ctx, context, tool=tool, owner=owner)


def running_tool_body(ctx):
    return extensions().call_wrapper.running_tool_body(ctx)


def retain_full_output(ctx, output: str | None) -> None:
    extensions().call_wrapper.retain_full_output(ctx, output)


def retained_output(ctx, fallback: str) -> str:
    return extensions().call_wrapper.retained_output(ctx, fallback)
