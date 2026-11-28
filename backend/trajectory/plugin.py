"""Trajectory recording and run timing as a kernel plugin.

``TrajectoryPlugin`` registers the implementations of the kernel's ports
(``agent.extensions``). Recording itself is unchanged: business code only
enqueues facts (fail-open), the trajectory worker persists them
(docs/trajectory-rearch/SPEC.md). Every method resolves the producer function
when it is called, as the kernel's call sites did, so the same function runs
with the same arguments in the same order.
"""
from __future__ import annotations

import asyncio
import os

from core.log import create_logger

# These lifecycle lines kept the logger they had in main.py.
log = create_logger("main")


def worker_mode(config) -> str:
    """TRAJECTORY_WORKER_MODE: external (default with JWT_SECRET), embedded (default without) or off.

    Off runs no emitter, metadata sync or worker in this process.
    """
    default = "external" if config.jwt_secret else "embedded"
    raw = (os.getenv("TRAJECTORY_WORKER_MODE") or "").strip().lower()
    if not raw:
        return default
    if raw in {"external", "embedded", "off"}:
        return raw
    log.warning(f"Invalid TRAJECTORY_WORKER_MODE={raw!r}; using {default!r}")
    return default


async def start_trajectory(app, mode: str) -> None:
    """Start the spool emitter, metadata sync and, in embedded mode, the in-process worker.

    Recording is fail-open: none of them can keep the business app from starting.
    """
    from trajectory.config import admin_enabled, enabled as trajectory_recording_enabled
    log.info(
        "Trajectory monitoring: recording=%s admin_read=%s worker=%s",
        trajectory_recording_enabled(),
        admin_enabled(),
        mode,
    )
    if mode == "off":
        return
    # The first get_emitter() creates the spool directory and the writer
    # thread; that file I/O belongs here, never on a request path.
    from trajectory.emitter import get_emitter
    get_emitter()
    try:
        from trajectory.meta_sync import start_meta_sync
        start_meta_sync()
    except Exception as e:
        log.warning(f"Trajectory metadata sync did not start: {type(e).__name__}")
    if mode == "embedded":
        try:
            from trajectory.worker.embedded import start_embedded_worker
            await start_embedded_worker(app)
        except Exception as e:
            log.warning(f"Embedded trajectory worker did not start: {type(e).__name__}: {e}")


async def shutdown_trajectory(mode: str) -> None:
    """Stop metadata sync, flush and close the emitter, then stop the embedded worker."""
    try:
        from trajectory.meta_sync import stop_meta_sync
        await stop_meta_sync()
    except Exception as e:
        log.warning(f"Error stopping trajectory metadata sync: {type(e).__name__}")
    try:
        from trajectory.emitter import get_emitter
        emitter = get_emitter()
        if emitter is not None:
            # close(5.0) joins the writer thread; keep the loop free meanwhile.
            await asyncio.to_thread(emitter.close, 5.0)
    except Exception as e:
        log.warning(f"Error closing trajectory emitter: {type(e).__name__}")
    if mode == "embedded":
        try:
            from trajectory.worker.embedded import stop_embedded_worker
            await stop_embedded_worker()
        except Exception as e:
            log.warning(f"Error stopping embedded trajectory worker: {type(e).__name__}")


class TrajectoryRecorder:
    """The turn observer (after commit, in process, at most once) and its identities."""

    async def record(self, type, data, *, context=None, db=None, event_id=None, occurred_at=None):
        from trajectory import record
        await record(type, data, context=context, db=db, event_id=event_id, occurred_at=occurred_at)

    def record_stream(self, context, event):
        from trajectory import record_stream
        return record_stream(context, event)

    def emit(self, type, data, *, context=None, event_id=None):
        from trajectory import emit
        return emit(type, data, context=context, event_id=event_id)

    def emit_control(self, control, *, db=None):
        from trajectory import emit_control
        return emit_control(control, db=db)

    def timestamp(self, value):
        from trajectory.types import iso
        return iso(value)

    def enabled(self, user_id=None):
        from trajectory import enabled
        return enabled(user_id)

    def current(self):
        from trajectory import current
        return current()

    def bind(self, context):
        from trajectory import bind
        return bind(context)

    def new_identity(self, user_id, session_id, **fields):
        from trajectory import TraceContext
        return TraceContext(user_id, session_id, **fields)

    def parse_identity(self, saved):
        from trajectory import TraceContext
        return TraceContext.parse(saved)

    def saved_identity(self, saved):
        from trajectory.producers import identity
        return identity(saved)

    def markers(self, saved):
        from trajectory.producers import markers
        return markers(saved)

    async def session_identity(self, db, user_id, session_id, **ids):
        from trajectory.producers import session_context
        return await session_context(db, user_id, session_id, **ids)

    async def activity_identity(self, db, user_id, session_id, *, saved=None, **ids):
        from trajectory.producers import activity_context
        return await activity_context(db, user_id, session_id, saved=saved, **ids)

    async def mark_recording_in_tx(self, db, execution, *, user_id, session_id, context):
        from trajectory.producers import mark_recording_in_tx
        await mark_recording_in_tx(db, execution, user_id=user_id, session_id=session_id, context=context)

    async def paused_in_tx(self, db, context):
        from trajectory.producers import paused_in_tx
        return await paused_in_tx(db, context)

    async def baseline_candidate_in_tx(self, db, *, user_id, session_id, context, pause=False):
        from trajectory.producers import baseline_candidate_in_tx
        return await baseline_candidate_in_tx(db, user_id=user_id, session_id=session_id,
                                              context=context, pause=pause)

    async def capture_asset_ids_in_tx(self, db, context, asset_ids, *, role):
        from trajectory.artifacts import capture_asset_ids_in_tx
        return await capture_asset_ids_in_tx(db, context, asset_ids, role=role)

    def context_of(self, ctx):
        from trajectory.capture import call_state
        return call_state(ctx, "context")

    def attach(self, ctx, context):
        from trajectory.capture import set_call_state
        set_call_state(ctx, "context", context)

    async def context_for_tool(self, ctx):
        from trajectory.capture import context_for_tool
        return await context_for_tool(ctx)

    def public_value(self, value):
        from trajectory.capture import public_value
        return public_value(value)

    def tool_schema(self, tool_info, name):
        from trajectory.capture import tool_schema
        return tool_schema(tool_info, name)

    def requested_tool_schema(self, ctx, name, tool_info=None):
        from trajectory.capture import requested_tool_schema
        return requested_tool_schema(ctx, name, tool_info)


class TrajectoryCalls:
    """The call wrapper: provider requests (``RequestCapture``) and tool output (``ToolOutputStream``)."""

    async def begin_model_call(self, ctx, *, purpose, model_id, payload, capture_level):
        from trajectory.capture import RequestCapture
        return await RequestCapture.start(ctx, purpose=purpose, model_id=model_id, payload=payload,
                                          capture_level=capture_level)

    def chunk_blocks(self, kind):
        from trajectory.capture import litellm_chunk_blocks, responses_chunk_blocks
        return {"litellm": litellm_chunk_blocks, "responses": responses_chunk_blocks}[kind]

    async def capture_billing(self, context, meter, usage, credits, purpose):
        from trajectory.capture import capture_billing
        await capture_billing(context, meter, usage, credits, purpose)

    def bind_model_billing(self, ctx, meter):
        from trajectory.capture import set_call_state
        set_call_state(ctx, "billing_event_id", getattr(meter, "event_id", None))
        set_call_state(ctx, "active_request", None)

    def active_request(self, ctx):
        from trajectory.capture import call_state
        return call_state(ctx, "active_request")

    def note_model_attempt(self, ctx, attempt):
        from trajectory.capture import set_call_state
        set_call_state(ctx, "attempt", attempt)

    def media_capture(self, ctx, enabled):
        from trajectory.capture import set_call_state
        sources = {} if enabled else None
        inline = {} if sources is not None else None
        set_call_state(ctx, "media_sources", sources)
        set_call_state(ctx, "inline_media", inline)
        return sources, inline

    def note_tool_request(self, ctx, tool_info, provider_call_id, arguments_raw):
        from trajectory.capture import set_call_state
        set_call_state(ctx, "tool_metadata", (tool_info, provider_call_id, arguments_raw))

    def pending_tool_request(self, ctx):
        from trajectory.capture import call_state
        return call_state(ctx, "tool_metadata", (None, None, None))

    def begin_tool_body(self, ctx, context, *, tool, owner, stopped=None):
        from trajectory.capture import set_call_state
        from trajectory.tool_output import ToolOutputStream
        set_call_state(ctx, "full_output", None)
        stream = ToolOutputStream(context, tool=tool, owner=owner, stopped=stopped)
        set_call_state(ctx, "output_stream", stream)
        return stream

    def tool_body(self, ctx, context, *, tool, owner):
        """The hooks' stream when they run this very call; a call made without them records into its own."""
        from trajectory.capture import call_state, set_call_state
        from trajectory.tool_output import ToolOutputStream
        stream = call_state(ctx, "output_stream")
        if stream is None or stream.closed or stream.owner is not owner or stream.context is not context:
            stream = ToolOutputStream(context, tool=tool, owner=owner)
            set_call_state(ctx, "output_stream", stream)
        return stream

    def running_tool_body(self, ctx):
        from trajectory.capture import call_state
        return call_state(ctx, "output_stream")

    def retain_full_output(self, ctx, output):
        from trajectory.capture import set_call_state
        set_call_state(ctx, "full_output", output)

    def retained_output(self, ctx, fallback):
        from trajectory.capture import call_state
        retained = call_state(ctx, "full_output")
        return retained if retained is not None else fallback


class TrajectoryPlugin:
    name = "trajectory"

    def __init__(self) -> None:
        self._mode: str | None = None

    def register(self, registry) -> None:
        from trajectory.latency import TrajectoryTiming
        registry.set_recorder(TrajectoryRecorder())
        registry.set_call_wrapper(TrajectoryCalls())
        registry.set_timing(TrajectoryTiming())

    def mount(self, app) -> None:
        """Decide the worker mode; in embedded mode the backend serves the admin trajectory API."""
        from core.config import get_config
        app.state.trajectory_worker_mode = worker_mode(get_config())
        if app.state.trajectory_worker_mode == "embedded":
            # Otherwise the trajectory worker serves the admin trajectory API and
            # socket, and nothing here may import the worker or the trace store.
            try:
                from trajectory.worker.embedded import mount_admin_routers
                mount_admin_routers(app)
            except Exception as e:
                # Trajectory administration is fail-open like recording: it never
                # keeps the business app from being built.
                log.warning(f"Admin trajectory routes are unavailable: {type(e).__name__}: {e}")

    async def startup(self, app) -> None:
        from core.config import get_config
        self._mode = getattr(app.state, "trajectory_worker_mode", None) or worker_mode(get_config())
        await start_trajectory(app, self._mode)

    async def shutdown(self, app) -> None:
        from core.config import get_config
        await shutdown_trajectory(self._mode or worker_mode(get_config()))
