import '../api/voice_events.dart';
import 'voice_call_state.dart';

/// Seconds since `ready`, on this device's clock.
int callElapsedSeconds(VoiceCallState state, DateTime now) {
  final start = state.connectedAt;
  if (start == null) return 0;
  final seconds = now.difference(start).inSeconds;
  return seconds < 0 ? 0 : seconds;
}

/// (state, server event) → state. Pure: the controller does the side
/// effects (audio, tones, closing the socket) around it, so both clients
/// read a call the same way (docs/VOICE_CALL_SPEC.md §4.1, §5.3).
VoiceCallState reduceVoiceEvent(
  VoiceCallState state,
  VoiceServerEvent event, {
  required DateTime now,
}) {
  switch (event) {
    case VoiceReadyEvent():
      if (state.status != VoiceCallStatus.connecting) return state;
      return state.copyWith(
        status: VoiceCallStatus.connected,
        phase: VoicePhase.greeting,
        working: false,
        late: false,
        callId: event.callId,
        maxSeconds: event.maxSeconds,
        connectedAt: now,
      );
    case VoicePhaseEvent():
      if (!state.live) return state;
      return state.copyWith(
        phase: event.phase,
        working: event.working || event.phase == VoicePhase.working,
        late: event.late,
      );
    case VoiceTurnEvent():
      if (state.status == VoiceCallStatus.idle || event.turnId.isEmpty) {
        return state;
      }
      return state.copyWith(turns: {...state.turns, event.turnId: event.turn});
    case VoiceCostEvent():
      if (state.status == VoiceCallStatus.idle) return state;
      if (state.status == VoiceCallStatus.ended) {
        return state.copyWith(
          cost: event.cost,
          end: state.end?.merge(cost: event.cost),
        );
      }
      return state.copyWith(cost: event.cost);
    case VoiceLimitEvent():
      // The closing phrase still plays; `ended` follows it.
      if (!state.live) return state;
      return state.copyWith(
        status: VoiceCallStatus.ending,
        endingReason: VoiceEndReason.limit,
        pauseCause: null,
      );
    case VoiceErrorEvent():
      if (!state.active) return state;
      // A failure while already hanging up is still the user's hang-up.
      final closing = state.endingReason;
      return endVoiceCall(
        state,
        closing ?? VoiceEndReason.error,
        now: now,
        message: closing == null ? event.message : null,
      );
    case VoiceEndedEvent():
      if (state.status == VoiceCallStatus.idle) return state;
      final end = state.end;
      if (state.status == VoiceCallStatus.ended && end != null) {
        // The call already ended here (an error, a timeout): keep why, take
        // the server's numbers.
        return state.copyWith(
          cost: event.cost ?? state.cost,
          end: end.merge(
            durationSeconds: event.durationSeconds,
            pendingTurns: event.pendingTurns,
            cost: event.cost,
          ),
        );
      }
      final cost = event.cost ?? state.cost;
      return state.copyWith(
        status: VoiceCallStatus.ended,
        cost: cost,
        endingReason: null,
        pauseCause: null,
        end: VoiceCallEnd(
          reason: event.reason,
          durationSeconds: event.durationSeconds,
          pendingTurns: event.pendingTurns,
          cost: cost,
        ),
      );
    case VoicePlaybackClearEvent():
    case VoicePhraseEvent():
    case VoiceHeartbeatEvent():
    case VoiceUnknownEvent():
      return state;
  }
}

/// A call this device ends by itself (no `ended` from the server): the
/// duration and open requests come from what it saw.
VoiceCallState endVoiceCall(
  VoiceCallState state,
  VoiceEndReason reason, {
  required DateTime now,
  String? message,
  String? detailKey,
}) {
  if (state.status == VoiceCallStatus.ended ||
      state.status == VoiceCallStatus.idle) {
    return state;
  }
  return state.copyWith(
    status: VoiceCallStatus.ended,
    endingReason: null,
    pauseCause: null,
    end: VoiceCallEnd(
      reason: reason,
      durationSeconds: callElapsedSeconds(state, now),
      pendingTurns: state.openTurns,
      cost: state.cost,
      message: message,
      detailKey: detailKey,
    ),
  );
}

/// How a socket that closed before `ended` ends the call (§5.5). A closing
/// that was already under way (hang-up, time limit) keeps its reason.
({VoiceEndReason reason, String? detailKey}) voiceCloseOutcome(
  int? code,
  VoiceCallState state,
) {
  final closing = state.endingReason;
  if (closing != null) return (reason: closing, detailKey: null);
  return switch (code) {
    4009 => (reason: VoiceEndReason.concurrent, detailKey: null),
    4029 => (reason: VoiceEndReason.quota, detailKey: null),
    4503 => (reason: VoiceEndReason.error, detailKey: 'voice:errors.disabled'),
    4404 => (
      reason: VoiceEndReason.error,
      detailKey: 'voice:errors.assistantUnavailable',
    ),
    4001 || 1011 => (
      reason: VoiceEndReason.error,
      detailKey: 'voice:errors.connectFailed',
    ),
    4003 || 4400 || 1000 => (reason: VoiceEndReason.error, detailKey: null),
    _ when state.live => (reason: VoiceEndReason.network, detailKey: null),
    _ => (
      reason: VoiceEndReason.error,
      detailKey: 'voice:errors.connectFailed',
    ),
  };
}
