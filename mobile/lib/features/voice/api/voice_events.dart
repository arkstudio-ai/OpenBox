import 'dart:convert';

import '../../../shared/models/json.dart';
import '../state/voice_call_state.dart';

/// Server → client JSON events of `/ws/assistant/voice`
/// (docs/VOICE_CALL_SPEC.md §5.3). Field names are the contract's; unknown
/// types parse to [VoiceUnknownEvent] and are ignored.
sealed class VoiceServerEvent {
  const VoiceServerEvent();

  /// Null for a frame that is not a JSON object with a `type`.
  static VoiceServerEvent? decode(String raw) {
    Object? parsed;
    try {
      parsed = jsonDecode(raw);
    } on FormatException {
      return null;
    }
    return parsed is Map<String, dynamic> ? fromJson(parsed) : null;
  }

  static VoiceServerEvent? fromJson(Map<String, dynamic> json) {
    final type = asString(json['type']);
    return switch (type) {
      null => null,
      'ready' => VoiceReadyEvent(
        callId: asString(json['call_id']) ?? '',
        maxSeconds: asInt(json['max_seconds']),
        outputSampleRate: asInt(json['output_sample_rate']) ?? 24000,
      ),
      'phase' => VoicePhaseEvent(
        phase:
            VoicePhase.values.asNameMap()[asString(json['value'])] ??
            VoicePhase.listening,
        working: json['working'] == true,
        late: json['late'] == true,
      ),
      'playback.clear' => const VoicePlaybackClearEvent(),
      'phrase' => VoicePhraseEvent(asString(json['key']) ?? ''),
      'turn' => VoiceTurnEvent(
        turnId: asString(json['turn_id']) ?? '',
        turn: VoiceTurn(
          state: asString(json['state']) ?? 'accepted',
          inboxId: asString(json['inbox_id']),
          messageId: asString(json['message_id']),
        ),
      ),
      'cost' => VoiceCostEvent(VoiceCost.fromJson(json)),
      'heartbeat' => VoiceHeartbeatEvent(asInt(json['elapsed_seconds']) ?? 0),
      'limit' => VoiceLimitEvent(asString(json['reason']) ?? 'max_duration'),
      'error' => VoiceErrorEvent(
        code: asString(json['code']) ?? 'internal',
        message: asString(json['message']),
      ),
      'ended' => VoiceEndedEvent(
        reason: VoiceEndReason.parse(json['reason']),
        durationSeconds: asInt(json['duration_seconds']) ?? 0,
        pendingTurns: asInt(json['pending_turns']) ?? 0,
        cost: json['cost'] is Map<String, dynamic>
            ? VoiceCost.fromJson(json['cost'] as Map<String, dynamic>)
            : null,
      ),
      _ => VoiceUnknownEvent(type),
    };
  }
}

/// Handshake done; audio may flow from now on.
final class VoiceReadyEvent extends VoiceServerEvent {
  const VoiceReadyEvent({
    required this.callId,
    this.maxSeconds,
    this.outputSampleRate = 24000,
  });

  final String callId;
  final int? maxSeconds;
  final int outputSampleRate;
}

final class VoicePhaseEvent extends VoiceServerEvent {
  const VoicePhaseEvent({
    required this.phase,
    this.working = false,
    this.late = false,
  });

  final VoicePhase phase;
  final bool working;
  final bool late;
}

/// The user started talking or a reply was cancelled: drop queued audio.
final class VoicePlaybackClearEvent extends VoiceServerEvent {
  const VoicePlaybackClearEvent();
}

/// A fixed phrase started (its audio arrives as ordinary frames). Only for
/// timing and debugging; nothing on screen shows it.
final class VoicePhraseEvent extends VoiceServerEvent {
  const VoicePhraseEvent(this.key);

  final String key;
}

final class VoiceTurnEvent extends VoiceServerEvent {
  const VoiceTurnEvent({required this.turnId, required this.turn});

  final String turnId;
  final VoiceTurn turn;
}

final class VoiceCostEvent extends VoiceServerEvent {
  const VoiceCostEvent(this.cost);

  final VoiceCost cost;
}

final class VoiceHeartbeatEvent extends VoiceServerEvent {
  const VoiceHeartbeatEvent(this.elapsedSeconds);

  final int elapsedSeconds;
}

/// Time or credits are up (`max_duration` / `credits`): the closing phrase plays,
/// then `ended` follows.
final class VoiceLimitEvent extends VoiceServerEvent {
  const VoiceLimitEvent(this.reason);

  final String reason;
}

/// The server gave up; it closes the socket next.
final class VoiceErrorEvent extends VoiceServerEvent {
  const VoiceErrorEvent({required this.code, this.message});

  final String code;
  final String? message;
}

final class VoiceEndedEvent extends VoiceServerEvent {
  const VoiceEndedEvent({
    required this.reason,
    this.durationSeconds = 0,
    this.pendingTurns = 0,
    this.cost,
  });

  final VoiceEndReason reason;
  final int durationSeconds;
  final int pendingTurns;
  final VoiceCost? cost;
}

final class VoiceUnknownEvent extends VoiceServerEvent {
  const VoiceUnknownEvent(this.type);

  final String type;
}
