import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';
import '../../../shared/models/session.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/toast.dart';
import '../../../shared/ws/ws_client.dart';
import 'message_equality.dart';

/// Streaming chat state — a 1:1 port of frontend-v2
/// `features/chat/stores/stream.ts` reducers plus the `useChatEvents` WS
/// dispatch. Physically isolated from REST fetching (web §7.4).
class ChatStreamState {
  const ChatStreamState({
    this.messages = const {},
    this.status = const {},
    this.retry = const {},
    this.runError = const {},
  });

  final Map<String, List<ChatMessage>> messages;
  final Map<String, SessionStatus> status;

  /// Which retry a stalled run is on, so the wait can account for itself.
  final Map<String, RetryProgress> retry;

  /// Why the last run failed, shown above the composer until the next send.
  final Map<String, String> runError;

  List<ChatMessage> messagesOf(String sessionId) =>
      messages[sessionId] ?? const [];

  SessionStatus? statusOf(String sessionId) => status[sessionId];

  RetryProgress? retryOf(String sessionId) => retry[sessionId];

  String? runErrorOf(String sessionId) => runError[sessionId];

  /// The newest message the server has confirmed for [sessionId] — where a
  /// live catch-up reads on from. Optimistic echoes are skipped: their ids
  /// mean nothing to the server.
  String? newestHistoryId(String sessionId) => _historyEdge(sessionId, 1);

  /// The oldest confirmed message held — where the next older page ends.
  String? oldestHistoryId(String sessionId) => _historyEdge(sessionId, -1);

  String? _historyEdge(String sessionId, int direction) {
    String? edge;
    for (final message in messagesOf(sessionId)) {
      if (isOptimisticMessage(message)) continue;
      if (edge == null || message.id.compareTo(edge).sign == direction) {
        edge = message.id;
      }
    }
    return edge;
  }

  ChatStreamState copyWith({
    Map<String, List<ChatMessage>>? messages,
    Map<String, SessionStatus>? status,
    Map<String, RetryProgress>? retry,
    Map<String, String>? runError,
  }) => ChatStreamState(
    messages: messages ?? this.messages,
    status: status ?? this.status,
    retry: retry ?? this.retry,
    runError: runError ?? this.runError,
  );
}

/// Web `isBusyStatus`: busy | finalizing | retry | compacting.
bool isBusyStatus(SessionStatus? status) =>
    status == SessionStatus.busy ||
    status == SessionStatus.finalizing ||
    status == SessionStatus.retry ||
    status == SessionStatus.compacting;

/// An echo `send` shows before the server confirms the message. Its id is
/// `tmp-<client message id>` and exists only in this store; the server's copy
/// replaces it by client message id, over the socket
/// ([ChatStreamStore.addMessage]) or in a history read
/// ([ChatStreamStore.mergeHistory]).
bool isOptimisticMessage(ChatMessage message) => message.id.startsWith('tmp-');

int _toolRank(ToolStatus s) => switch (s) {
  ToolStatus.pending => 0,
  ToolStatus.running => 1,
  ToolStatus.waitingInput => 2,
  ToolStatus.completed || ToolStatus.error => 3,
};

class ChatStreamStore extends Notifier<ChatStreamState> {
  StreamSubscription<WsEvent>? _sub;

  @override
  ChatStreamState build() {
    _sub?.cancel();
    _sub = ref.watch(wsClientProvider).events.listen(_onWsEvent);
    ref.onDispose(() => _sub?.cancel());
    return const ChatStreamState();
  }

  void _onWsEvent(WsEvent event) {
    final sessionId = event.sessionId;
    if (sessionId == null) return;
    switch (event.type) {
      case 'message.created':
        final msg = asMap(event.data['message']);
        if (msg.isNotEmpty) addMessage(sessionId, ChatMessage.fromJson(msg));
      case 'message.updated':
        final msg = asMap(event.data['message']);
        if (msg.isNotEmpty) updateMessage(sessionId, msg);
      case 'message.text_delta' || 'part.delta':
        appendPartDelta(
          sessionId,
          asString(event.data['messageId']) ?? '',
          asString(event.data['partId']) ?? '',
          asString(event.data['text']) ?? asString(event.data['delta']) ?? '',
        );
      case 'part.created':
        final part = asMap(event.data['part']);
        if (part.isNotEmpty) {
          addPart(
            sessionId,
            asString(event.data['messageId']) ?? '',
            MessagePart.fromJson(part),
          );
        }
      case 'part.updated':
        final part = asMap(event.data['part']);
        if (part.isNotEmpty) {
          updatePart(
            sessionId,
            asString(event.data['messageId']) ?? '',
            MessagePart.fromJson(part),
          );
        }
      case 'tool.running':
        updateToolStatus(
          sessionId,
          asString(event.data['partId']) ?? '',
          ToolStatus.running,
          event.data,
        );
      case 'tool.completed':
        updateToolStatus(
          sessionId,
          asString(event.data['partId']) ?? '',
          ToolStatus.completed,
          event.data,
        );
      case 'tool.error':
        updateToolStatus(
          sessionId,
          asString(event.data['partId']) ?? '',
          ToolStatus.error,
          event.data,
        );
      case 'session.status':
        final status = sessionStatusFrom(asString(event.data['status']));
        setStatus(sessionId, status);
        // A fresh run supersedes whatever the last one failed with.
        if (status == SessionStatus.busy) clearRunError(sessionId);
        final attempt = asInt(event.data['attempt']);
        if (status == SessionStatus.retry && attempt != null && attempt > 0) {
          setRetry(
            sessionId,
            attempt,
            asInt(event.data['maxAttempts']) ?? attempt,
          );
        }
      case 'session.finalizing':
        setStatus(sessionId, SessionStatus.finalizing);
      case 'session.error':
        setStatus(sessionId, SessionStatus.error);
        // Say it twice, deliberately. The toast is what someone sees if they
        // are looking; the line above the composer is what remains for
        // someone who was not, or who dismissed the toast — without it a
        // failed run leaves a screen that looks exactly like a working one.
        final message = runFailureText(
          ref.read(i18nProvider),
          asMap(event.data['error']),
        );
        ref.read(toastProvider.notifier).error(message);
        setRunError(sessionId, message);
    }
  }

  /// A contiguous stretch of the server's history landed — the newest
  /// window, a catch-up from the newest held message, or an older page —
  /// oldest message first (web `mergeSnapshotMessages`, made range-aware).
  ///
  /// Ids ascend with creation time, so the first and last incoming ids bound
  /// the stretch the read speaks for:
  /// - held messages older than the first stay exactly as they are, which is
  ///   how pages loaded by scrolling up survive every refresh;
  /// - held messages inside the bounds merge with their incoming copy by id
  ///   ([_mergeMessage]: streamed state never moves backwards). One the read
  ///   lacks was deleted on the server, and goes;
  /// - held messages newer than the last — socket arrivals after the read
  ///   started — stay after it, and so do optimistic echoes the read does not
  ///   confirm. One it does confirm is matched by client message id and
  ///   replaced where the server put it, by the server's copy as it stands.
  ///
  /// A message the read left unchanged keeps its instance, and a read that
  /// changed nothing publishes nothing, so a poll that finds nothing new
  /// rebuilds nothing.
  void mergeHistory(String sessionId, List<ChatMessage> incoming) {
    // No bounds to speak for; an empty snapshot never removed anything either.
    if (incoming.isEmpty) return;
    final held = state.messagesOf(sessionId);
    final first = incoming.first.id;
    final last = incoming.last.id;
    bool isOlder(ChatMessage m) =>
        !isOptimisticMessage(m) && m.id.compareTo(first) < 0;
    bool isNewer(ChatMessage m) =>
        isOptimisticMessage(m) || m.id.compareTo(last) > 0;

    final byId = <String, ChatMessage>{};
    final byClientId = <String, ChatMessage>{};
    for (final m in held) {
      if (isOlder(m)) continue;
      byId[m.id] = m;
      final clientId = m.clientMessageId;
      if (clientId != null) byClientId[clientId] = m;
    }
    final used = Set<ChatMessage>.identity();
    final merged = [...held.where(isOlder)];
    for (final message in incoming) {
      final clientId = message.clientMessageId;
      final match =
          byId[message.id] ?? (clientId == null ? null : byClientId[clientId]);
      // Found by client id, the held copy is another message — the send's
      // echo — not an earlier state of this one, so it is used up but not
      // merged. Merging kept the echo's part, whose id only this store knows,
      // beside the server's own, and the bubble said everything twice.
      merged.add(
        match != null && used.add(match) && match.id == message.id
            ? _mergeMessage(match, message)
            : message,
      );
    }
    merged.addAll(held.where((m) => !used.contains(m) && isNewer(m)));
    if (_sameInstances(merged, held)) return;
    _setSessionMessages(sessionId, merged);
  }

  ChatMessage _mergeMessage(ChatMessage live, ChatMessage snap) {
    final liveParts = {for (final p in live.parts) p.id: p};
    final usedParts = <String>{};
    final parts = <MessagePart>[];
    for (final snapPart in snap.parts) {
      final livePart = liveParts[snapPart.id];
      if (livePart == null) {
        parts.add(snapPart);
        continue;
      }
      usedParts.add(livePart.id);
      final part = _mergePart(livePart, snapPart);
      // A fresh copy of what is already held keeps the held instance.
      parts.add(samePart(part, livePart) ? livePart : part);
    }
    for (final p in live.parts) {
      if (!usedParts.contains(p.id)) parts.add(p);
    }
    if (_sameInstances(parts, live.parts) && sameMessageFields(live, snap)) {
      return live;
    }
    return snap.copyWith(parts: parts);
  }

  static bool _sameInstances<T>(List<T> a, List<T> b) {
    if (a.length != b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if (!identical(a[i], b[i])) return false;
    }
    return true;
  }

  MessagePart _mergePart(MessagePart live, MessagePart snap) {
    // text/reasoning are append-only → longer string wins.
    if (live is TextPart && snap is TextPart) {
      return live.text.length >= snap.text.length ? live : snap;
    }
    if (live is ReasoningPart && snap is ReasoningPart) {
      return live.text.length >= snap.text.length ? live : snap;
    }
    // tool → higher status rank wins (never revert completed to spinner).
    if (live is ToolPart && snap is ToolPart) {
      return _toolRank(live.status) > _toolRank(snap.status) ? live : snap;
    }
    if (live is SuggestionsPart && snap is SuggestionsPart) {
      return live.status != SuggestionStatus.pending &&
              snap.status == SuggestionStatus.pending
          ? live
          : snap;
    }
    return snap;
  }

  /// `message.created` — upsert; replaces the optimistic `tmp-…` message
  /// matched by client_message_id.
  void addMessage(String sessionId, ChatMessage message) {
    final list = List<ChatMessage>.of(state.messagesOf(sessionId));
    final cmid = message.clientMessageId;
    final tmpIndex = cmid == null
        ? -1
        : list.indexWhere(
            (m) =>
                m.id == 'tmp-$cmid' ||
                (m.id != message.id && m.clientMessageId == cmid),
          );
    if (tmpIndex != -1) {
      list[tmpIndex] = message;
    } else {
      final existing = list.indexWhere((m) => m.id == message.id);
      if (existing != -1) {
        list[existing] = message;
      } else {
        list.add(message);
      }
    }
    _setSessionMessages(sessionId, list);
  }

  /// `message.updated` — shallow-merge partial fields, parts untouched.
  void updateMessage(String sessionId, Map<String, dynamic> partial) {
    final id = asString(partial['id']);
    if (id == null) return;
    _patchMessage(sessionId, id, (m) => m.mergePartial(partial));
  }

  void appendPartDelta(
    String sessionId,
    String messageId,
    String partId,
    String delta,
  ) {
    if (delta.isEmpty) return;
    _patchMessage(sessionId, messageId, (m) {
      final parts = [
        for (final p in m.parts)
          if (p.id != partId)
            p
          else if (p is TextPart)
            p.appendDelta(delta)
          else if (p is ReasoningPart)
            p.appendDelta(delta)
          else
            p,
      ];
      return m.copyWith(parts: parts);
    });
  }

  void addPart(String sessionId, String messageId, MessagePart part) {
    if (part is SuggestionsPart) {
      updatePart(sessionId, messageId, part);
      return;
    }
    _patchMessage(sessionId, messageId, (m) {
      if (m.parts.any((p) => p.id == part.id)) return m;
      return m.copyWith(parts: [...m.parts, part]);
    });
  }

  void updatePart(String sessionId, String messageId, MessagePart part) {
    _patchMessage(sessionId, messageId, (m) {
      final index = m.parts.indexWhere((p) => p.id == part.id);
      if (index == -1) return m.copyWith(parts: [...m.parts, part]);
      final parts = List<MessagePart>.of(m.parts);
      // Keep the longer streamed text if the update raced a delta.
      parts[index] = _mergePart(parts[index], part);
      return m.copyWith(parts: parts);
    });
  }

  /// `tool.running/completed/error` — patch the tool part wherever it lives.
  void updateToolStatus(
    String sessionId,
    String partId,
    ToolStatus status,
    Map<String, dynamic> data,
  ) {
    final list = state.messagesOf(sessionId);
    for (final message in list) {
      final index = message.parts.indexWhere((p) => p.id == partId);
      if (index == -1) continue;
      final part = message.parts[index];
      if (part is! ToolPart) return;
      if (_toolRank(status) < _toolRank(part.status)) return;
      final next = ToolPart(
        id: part.id,
        tool: asString(data['tool']) ?? part.tool,
        status: status,
        input: data['input'] ?? part.input,
        output: data['output'] ?? part.output,
        error: asString(data['error']) ?? part.error,
        title: asString(data['title']) ?? part.title,
        duration: asDouble(data['duration']) ?? part.duration,
        metadata: part.metadata,
      );
      final parts = List<MessagePart>.of(message.parts)..[index] = next;
      _patchMessage(sessionId, message.id, (m) => m.copyWith(parts: parts));
      return;
    }
  }

  void setStatus(String sessionId, SessionStatus status) {
    // Leaving a stale attempt behind would have the next wait open on
    // "retry 5 of 5" before anything had gone wrong.
    final retry = Map<String, RetryProgress>.of(state.retry);
    if (status != SessionStatus.retry) retry.remove(sessionId);
    state = state.copyWith(
      status: {...state.status, sessionId: status},
      retry: retry,
    );
  }

  void setRetry(String sessionId, int attempt, int maxAttempts) {
    state = state.copyWith(
      retry: {
        ...state.retry,
        sessionId: RetryProgress(attempt: attempt, maxAttempts: maxAttempts),
      },
    );
  }

  void setRunError(String sessionId, String message) {
    state = state.copyWith(runError: {...state.runError, sessionId: message});
  }

  void clearRunError(String sessionId) {
    if (!state.runError.containsKey(sessionId)) return;
    final runError = Map<String, String>.of(state.runError)..remove(sessionId);
    state = state.copyWith(runError: runError);
  }

  /// Take back an optimistic message whose send was rejected. Only ever
  /// removes the temp echo — a server-confirmed message with the same client
  /// id must survive, or a slow success would erase itself.
  void dropOptimistic(String sessionId, String clientMessageId) {
    final list = state.messagesOf(sessionId);
    final next = list
        .where(
          (m) =>
              !(isOptimisticMessage(m) && m.clientMessageId == clientMessageId),
        )
        .toList();
    if (next.length == list.length) return;
    _setSessionMessages(sessionId, next);
  }

  /// Forget what the server said about a session — regenerate or dismiss
  /// deleted part of it, or a history read's anchor vanished — so the next
  /// window starts clean. Optimistic echoes stay: they are not the server's
  /// to take back, and the next read confirms or keeps them as usual.
  void resetHistory(String sessionId) {
    final list = state.messagesOf(sessionId);
    final kept = list.where(isOptimisticMessage).toList();
    if (kept.length == list.length) return;
    final messages = Map<String, List<ChatMessage>>.of(state.messages);
    if (kept.isEmpty) {
      messages.remove(sessionId);
    } else {
      messages[sessionId] = kept;
    }
    state = state.copyWith(messages: messages);
  }

  void _patchMessage(
    String sessionId,
    String messageId,
    ChatMessage Function(ChatMessage) fn,
  ) {
    final list = state.messagesOf(sessionId);
    final index = list.indexWhere((m) => m.id == messageId);
    if (index == -1) return;
    final next = List<ChatMessage>.of(list)..[index] = fn(list[index]);
    _setSessionMessages(sessionId, next);
  }

  void _setSessionMessages(String sessionId, List<ChatMessage> list) {
    state = state.copyWith(messages: {...state.messages, sessionId: list});
  }
}

final chatStreamProvider = NotifierProvider<ChatStreamStore, ChatStreamState>(
  ChatStreamStore.new,
);
