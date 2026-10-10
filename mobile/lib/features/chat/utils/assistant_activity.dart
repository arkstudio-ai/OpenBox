import '../../../shared/models/message_part.dart';

/// What the personal assistant is doing right now, in words, while a turn
/// runs (web `lib/assistant-activity.ts`). The raw tool calls stay on the
/// server; the page says "正在查看进展…".
final _activity = <(RegExp, String)>[
  (RegExp(r'^tasks\.(submit|followup|next_step)$'), 'delegating'),
  (
    RegExp(r'^tasks\.(pause|resume|cancel|archive|link_existing)$'),
    'updatingTask',
  ),
  (
    RegExp(
      r'^(tasks\.(list|get)|results\.read|history\.read|sessions\.list|projects\.list)$',
    ),
    'checkingWork',
  ),
  (
    RegExp(r'^memory\.remember$|^memory\.update$|^decisions\.propose$'),
    'remembering',
  ),
  (RegExp(r'^memory\.'), 'recalling'),
  (RegExp(r'^knowledge\.'), 'reading'),
  (RegExp(r'^requests\.'), 'checkingRequests'),
  (RegExp(r'^schedules\.|^briefing\.'), 'scheduling'),
  (RegExp(r'^projects\.brief\.'), 'updatingBrief'),
  (RegExp(r'^assets\.'), 'handlingFiles'),
  (RegExp(r'^status\.'), 'checkingStatus'),
  (RegExp(r'^sessions\.rename$'), 'updatingTask'),
];

bool _inFlight(MessagePart part) => switch (part) {
  ToolPart(:final status) =>
    status == ToolStatus.running || status == ToolStatus.pending,
  SubtaskPart(:final status) => status == 'running' || status == 'pending',
  _ => false,
};

/// i18n key suffix under `chat:assistant.activity` for the call in flight,
/// else the last one. [calls] are the turn's tool and subtask parts, in
/// stream order.
String assistantActivity(List<MessagePart> calls) {
  final running = calls.where(_inFlight).toList();
  final current = running.lastOrNull ?? calls.lastOrNull;
  if (current is! ToolPart) return 'thinking';
  for (final (pattern, key) in _activity) {
    if (pattern.hasMatch(current.tool)) return key;
  }
  return 'working';
}
