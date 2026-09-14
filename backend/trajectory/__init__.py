"""Durable, session-centered execution recording for every user.

Business code only enqueues facts (fail-open); the trajectory worker persists them.
"""
from trajectory.config import enabled
from trajectory.context import TraceContext, bind, current
from trajectory.emitter import emit, emit_after_commit, emit_control, emit_stream, get_emitter
from trajectory.recorder import context_for_session, flush, record, record_stream
from trajectory.types import TrajectoryError, RecordingError

__all__ = ["TraceContext", "bind", "current", "enabled", "context_for_session",
           "record", "record_stream", "flush",
           "emit", "emit_after_commit", "emit_control", "emit_stream", "get_emitter",
           "TrajectoryError", "RecordingError"]
