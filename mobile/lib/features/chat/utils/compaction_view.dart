import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';

enum CompactionStatus { running, completed, failed, interrupted }

class CompactionView {
  const CompactionView({
    required this.id,
    required this.status,
    required this.summary,
  });

  final String id;
  final CompactionStatus status;
  final String summary;
}

CompactionPart? compactionPart(ChatMessage message) =>
    message.parts.whereType<CompactionPart>().firstOrNull;

bool isCompactionRequest(ChatMessage message) =>
    message.isUser &&
    (message.agent == 'compaction' || compactionPart(message) != null);

// The agent identifies streaming summaries before summary=true is persisted.
bool isCompactionMessage(ChatMessage message) =>
    isCompactionRequest(message) ||
    (message.isAssistant &&
        (message.agent == 'compaction' || message.summary == true));

/// Stable request identities survive streaming, history paging and retries.
List<CompactionView> buildCompactionViews(
  List<ChatMessage> messages,
  bool streaming,
) {
  final requests = <String, ChatMessage>{};
  final summaries = <String, ChatMessage>{};
  final ids = <String>{};
  for (final message in messages.where(isCompactionMessage)) {
    final request = isCompactionRequest(message);
    final id = request ? message.id : (message.parentId ?? message.id);
    ids.add(id);
    (request ? requests : summaries)[id] = message;
  }
  return [
    for (final id in ids)
      _view(id, requests[id], summaries[id], messages.lastOrNull, streaming),
  ];
}

CompactionView _view(
  String id,
  ChatMessage? request,
  ChatMessage? attempt,
  ChatMessage? last,
  bool streaming,
) {
  final descriptor = request == null ? null : compactionPart(request);
  final text = attempt?.parts.whereType<TextPart>().map((p) => p.text).join();
  final failed = attempt?.error != null || attempt?.finish == 'error';
  final completed =
      !failed &&
      (attempt?.finish == 'stop' ||
          (descriptor?.replacementId?.isNotEmpty ?? false) ||
          (descriptor?.summary?.isNotEmpty ?? false));
  final live =
      streaming &&
      (attempt ?? request)?.id == last?.id &&
      attempt?.finish == null;
  return CompactionView(
    id: id,
    summary: text != null && text.isNotEmpty ? text : descriptor?.summary ?? '',
    status: failed
        ? CompactionStatus.failed
        : completed
        ? CompactionStatus.completed
        : live
        ? CompactionStatus.running
        : CompactionStatus.interrupted,
  );
}
