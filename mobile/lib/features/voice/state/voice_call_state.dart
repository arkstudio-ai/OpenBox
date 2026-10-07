import 'package:flutter/foundation.dart';

/// Where a call is (docs/VOICE_CALL_SPEC.md §4.1, the same for web and
/// phone). `paused` is the phone's own: a phone call or the system took the
/// audio while the socket stays up.
enum VoiceCallStatus {
  idle,
  requestingMic,
  connecting,
  connected,
  paused,
  ending,
  ended,
}

/// What a connected call is doing. Only the server's `phase` event sets it;
/// the client never infers one.
enum VoicePhase { greeting, listening, thinking, speaking, working }

/// Why a call ended (`ended.reason`); [wire] is the protocol spelling.
enum VoiceEndReason {
  hangup('hangup'),
  error('error'),
  limit('limit'),
  quota('quota'),
  concurrent('concurrent'),
  micLost('mic_lost'),
  network('network'),
  unsupported('unsupported'),
  micDenied('mic_denied'),
  micMissing('mic_missing'),
  micBusy('mic_busy');

  const VoiceEndReason(this.wire);

  final String wire;

  /// An unknown reason from a newer server reads as an error.
  static VoiceEndReason parse(Object? value) => values.firstWhere(
    (reason) => reason.wire == value,
    orElse: () => VoiceEndReason.error,
  );
}

/// What took the audio away while the call is [VoiceCallStatus.paused].
enum VoicePauseCause { interruption, background }

double _yuan(Object? value) => switch (value) {
  final num number => number.toDouble(),
  final String text => double.tryParse(text) ?? 0,
  _ => 0,
};

int _count(Object? value) => value is num ? value.toInt() : 0;

/// The server's running bill for the call (`cost`, the demo meter's
/// snapshot). The client only shows it.
@immutable
class VoiceCost {
  const VoiceCost({
    this.totalYuan = 0,
    this.costsYuan = const {},
    this.settledRounds = 0,
    this.unreportedRounds = 0,
    this.pending = false,
    this.isFinal = false,
  });

  factory VoiceCost.fromJson(Map<String, dynamic> json) {
    final costs = json['costs_yuan'];
    return VoiceCost(
      totalYuan: _yuan(json['total_yuan']),
      costsYuan: {
        if (costs is Map)
          for (final part in parts) part: _yuan(costs[part]),
      },
      settledRounds: _count(json['settled_rounds']),
      unreportedRounds: _count(json['unreported_rounds']),
      pending: json['pending'] == true,
      isFinal: json['final'] == true,
    );
  }

  /// The four billed parts, in the order the demo lists them.
  static const parts = [
    'input_text',
    'input_audio',
    'output_text',
    'output_audio',
  ];

  final double totalYuan;
  final Map<String, double> costsYuan;
  final int settledRounds;
  final int unreportedRounds;

  /// The latest round has no usage yet.
  final bool pending;
  final bool isFinal;

  /// Some interrupted rounds never reported usage: the total may be low.
  bool get partial => unreportedRounds > 0 || (isFinal && pending);
}

/// One request the call handed to the text assistant (`turn`).
@immutable
class VoiceTurn {
  const VoiceTurn({required this.state, this.inboxId, this.messageId});

  /// accepted / working / late / delivered / timeout / failed.
  final String state;
  final String? inboxId;
  final String? messageId;

  bool get open => state == 'accepted' || state == 'working' || state == 'late';
}

/// The last word on a finished call.
@immutable
class VoiceCallEnd {
  const VoiceCallEnd({
    required this.reason,
    this.durationSeconds = 0,
    this.pendingTurns = 0,
    this.cost,
    this.message,
    this.detailKey,
  });

  final VoiceEndReason reason;
  final int durationSeconds;

  /// Requests still being worked on; their results land in the conversation.
  final int pendingTurns;
  final VoiceCost? cost;

  /// `error.message` from the server, already in the user's language.
  final String? message;

  /// A local explanation (`voice:errors.*`) when the server sent none.
  final String? detailKey;

  /// Same rule as the web panel: quota and a call elsewhere cannot redial.
  bool get canRedial => const {
    VoiceEndReason.hangup,
    VoiceEndReason.error,
    VoiceEndReason.network,
    VoiceEndReason.limit,
  }.contains(reason);

  /// A late `ended` fills in what a local end could not know.
  VoiceCallEnd merge({
    int? durationSeconds,
    int? pendingTurns,
    VoiceCost? cost,
  }) => VoiceCallEnd(
    reason: reason,
    durationSeconds: durationSeconds ?? this.durationSeconds,
    pendingTurns: pendingTurns ?? this.pendingTurns,
    cost: cost ?? this.cost,
    message: message,
    detailKey: detailKey,
  );
}

const Object _keep = Object();

/// The whole call as the page, the call bar and the entry button see it.
/// Fields mirror the web `CallState`; `muted`, `speakerOn` and `expanded`
/// are local and never reported.
@immutable
class VoiceCallState {
  const VoiceCallState({
    this.status = VoiceCallStatus.idle,
    this.phase = VoicePhase.greeting,
    this.working = false,
    this.late = false,
    this.callId,
    this.maxSeconds,
    this.connectedAt,
    this.turns = const {},
    this.cost,
    this.end,
    this.endingReason,
    this.pauseCause,
    this.muted = false,
    this.speakerOn = true,
    this.expanded = false,
  });

  final VoiceCallStatus status;
  final VoicePhase phase;

  /// A request is still with the text assistant (shown beside the phase).
  final bool working;

  /// ... for longer than the server's `late_after_seconds`.
  final bool late;
  final String? callId;

  /// Seconds this call may last (`ready.max_seconds`, quota included).
  final int? maxSeconds;

  /// When `ready` arrived, on this device's clock.
  final DateTime? connectedAt;
  final Map<String, VoiceTurn> turns;
  final VoiceCost? cost;
  final VoiceCallEnd? end;

  /// Why the call is closing when the server, not the user, closes it
  /// (`limit` arrives before the closing phrase and `ended`).
  final VoiceEndReason? endingReason;
  final VoicePauseCause? pauseCause;
  final bool muted;
  final bool speakerOn;

  /// The full-screen call page is open.
  final bool expanded;

  /// A call exists: from the first tap until it has ended.
  bool get active =>
      status != VoiceCallStatus.idle && status != VoiceCallStatus.ended;

  /// The socket is up and past `ready`.
  bool get live =>
      status == VoiceCallStatus.connected || status == VoiceCallStatus.paused;

  /// Still dialling: the only control is cancel.
  bool get dialling =>
      status == VoiceCallStatus.requestingMic ||
      status == VoiceCallStatus.connecting;

  int get openTurns => turns.values.where((turn) => turn.open).length;

  VoiceCallState copyWith({
    VoiceCallStatus? status,
    VoicePhase? phase,
    bool? working,
    bool? late,
    Object? callId = _keep,
    Object? maxSeconds = _keep,
    Object? connectedAt = _keep,
    Map<String, VoiceTurn>? turns,
    Object? cost = _keep,
    Object? end = _keep,
    Object? endingReason = _keep,
    Object? pauseCause = _keep,
    bool? muted,
    bool? speakerOn,
    bool? expanded,
  }) => VoiceCallState(
    status: status ?? this.status,
    phase: phase ?? this.phase,
    working: working ?? this.working,
    late: late ?? this.late,
    callId: identical(callId, _keep) ? this.callId : callId as String?,
    maxSeconds: identical(maxSeconds, _keep)
        ? this.maxSeconds
        : maxSeconds as int?,
    connectedAt: identical(connectedAt, _keep)
        ? this.connectedAt
        : connectedAt as DateTime?,
    turns: turns ?? this.turns,
    cost: identical(cost, _keep) ? this.cost : cost as VoiceCost?,
    end: identical(end, _keep) ? this.end : end as VoiceCallEnd?,
    endingReason: identical(endingReason, _keep)
        ? this.endingReason
        : endingReason as VoiceEndReason?,
    pauseCause: identical(pauseCause, _keep)
        ? this.pauseCause
        : pauseCause as VoicePauseCause?,
    muted: muted ?? this.muted,
    speakerOn: speakerOn ?? this.speakerOn,
    expanded: expanded ?? this.expanded,
  );
}

/// `mm:ss`, or `h:mm:ss` past an hour.
String formatCallDuration(int seconds) {
  final safe = seconds < 0 ? 0 : seconds;
  final h = safe ~/ 3600;
  final m = (safe % 3600) ~/ 60;
  final s = safe % 60;
  String two(int v) => v.toString().padLeft(2, '0');
  return h > 0 ? '$h:${two(m)}:${two(s)}' : '${two(m)}:${two(s)}';
}

/// Yuan as the call shows it: four decimals, like the web control row.
String formatYuan(double yuan) => yuan.toStringAsFixed(4);
