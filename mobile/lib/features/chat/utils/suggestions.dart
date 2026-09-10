import '../../../shared/models/message_part.dart';
import '../../../shared/models/session.dart';
import 'turn_view.dart';

/// Only the newest successful answer can offer next steps. Never fall back
/// to an earlier turn while a new answer (or its suggestions) is arriving.
SuggestionsPart? latestSuggestions(
  List<ChatRow> rows,
  SessionStatus? status, {
  bool atBottom = true,
  bool readOnly = false,
  bool hasRunError = false,
  bool hasPendingInput = false,
}) {
  if (status != SessionStatus.idle ||
      !atBottom ||
      readOnly ||
      hasRunError ||
      hasPendingInput ||
      rows.isEmpty) {
    return null;
  }
  final last = rows.last;
  if (last is! AssistantTurnData ||
      last.finish != 'stop' ||
      last.error != null ||
      last.messages.last.summary == true) {
    return null;
  }
  final parts = last.messages.last.parts.whereType<SuggestionsPart>();
  if (parts.isEmpty) return null;
  final part = parts.last;
  if (part.id.isEmpty || part.status == SuggestionStatus.unavailable) {
    return null;
  }
  return part.status == SuggestionStatus.completed && part.items.isEmpty
      ? null
      : part;
}
