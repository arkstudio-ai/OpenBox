import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/models/json.dart';
import '../../../shared/models/message_part.dart';
import '../api/chat_api.dart';
import '../utils/tool_map.dart';
import 'stream_store.dart';

/// What a subagent is doing right now, for the row that spawned it
/// (web `useSubagentProgress`).
///
/// A `task` call used to render as one line — "调用工具 · 调用中" — for as
/// long as the child took, which is often minutes. Everything the subagent
/// did was already arriving in this client: a child session is a session, and
/// its message and part events stream over the same socket, keyed by its own
/// id. Nothing was reading them. So this reads them.
class SubagentProgress {
  const SubagentProgress({
    this.sessionId,
    this.agent,
    this.toolCount = 0,
    this.current,
    this.seconds = 0,
  });

  /// The child session, when the task announced one.
  final String? sessionId;

  /// Which agent was spawned, e.g. "explore".
  final String? agent;

  /// Calls the subagent has made.
  final int toolCount;

  /// The call it is making now, if any.
  final ToolPart? current;

  /// Seconds of tool time the child has accounted for.
  final double seconds;
}

/// Read the child session id a task tool recorded on its part.
String? childSessionOf(ToolPart part) {
  final value = asString(part.metadata['child_session_id']);
  return (value != null && value.isNotEmpty) ? value : null;
}

/// Live progress for one task call. Zeroed for a part that is not a task, or
/// whose child has not reported anything yet.
final subagentProgressProvider =
    Provider.family<SubagentProgress, ToolPart>((ref, part) {
  final sessionId = childSessionOf(part);
  final agent = asString(part.metadata['subagent_type']);
  if (sessionId == null) return SubagentProgress(agent: agent);

  final messages = ref.watch(chatStreamProvider).messagesOf(sessionId);
  if (messages.isEmpty) {
    // While the parent runs, the child's parts arrive over the socket and the
    // store fills itself. A conversation opened afterwards never saw those
    // events, so the child is fetched once — only when there is nothing yet,
    // so a live run is never disturbed by a refetch.
    ref.watch(_subagentBackfillProvider(sessionId));
    return SubagentProgress(sessionId: sessionId, agent: agent);
  }

  final tools = [
    for (final message in messages) ...message.parts.whereType<ToolPart>(),
  ];
  if (tools.isEmpty) {
    return SubagentProgress(sessionId: sessionId, agent: agent);
  }
  final running = tools
      .where((t) =>
          t.status == ToolStatus.running || t.status == ToolStatus.pending)
      .toList();
  return SubagentProgress(
    sessionId: sessionId,
    agent: agent,
    toolCount: tools.length,
    // The newest running call: that is the one it is on. Falling back to the
    // last finished one keeps the row from going blank in the gap between two
    // calls, which is where most of a subagent's time actually goes.
    current: running.isNotEmpty ? running.last : tools.last,
    seconds: tools.fold(0, (sum, t) => sum + (toolDuration(t) ?? 0)),
  );
});

/// One-shot backfill of a child session's transcript into the stream store.
final _subagentBackfillProvider =
    FutureProvider.family<void, String>((ref, sessionId) async {
  ref.keepAlive();
  final messages = await ref.read(chatApiProvider).listMessages(sessionId);
  if (messages.isEmpty) return;
  ref.read(chatStreamProvider.notifier).setMessages(sessionId, messages);
});
