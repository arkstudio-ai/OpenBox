import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/models/json.dart';
import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';
import '../../../shared/models/session.dart';
import '../../../shared/ws/ws_client.dart';

/// Streaming chat state — a 1:1 port of frontend-v2
/// `features/chat/stores/stream.ts` reducers plus the `useChatEvents` WS
/// dispatch. Physically isolated from REST fetching (web §7.4).
class ChatStreamState {
  const ChatStreamState({this.messages = const {}, this.status = const {}});

  final Map<String, List<ChatMessage>> messages;
  final Map<String, SessionStatus> status;

  List<ChatMessage> messagesOf(String sessionId) =>
      messages[sessionId] ?? const [];

  SessionStatus? statusOf(String sessionId) => status[sessionId];
}

/// Web `isBusyStatus`: busy | finalizing | retry | compacting.
bool isBusyStatus(SessionStatus? status) =>
    status == SessionStatus.busy ||
    status == SessionStatus.finalizing ||
    status == SessionStatus.retry ||
    status == SessionStatus.compacting;

int _toolRank(ToolStatus s) => switch (s) {
      ToolStatus.pending => 0,
      ToolStatus.running => 1,
      ToolStatus.completed || ToolStatus.error => 2,
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
          addPart(sessionId, asString(event.data['messageId']) ?? '',
              MessagePart.fromJson(part));
        }
      case 'part.updated':
        final part = asMap(event.data['part']);
        if (part.isNotEmpty) {
          updatePart(sessionId, asString(event.data['messageId']) ?? '',
              MessagePart.fromJson(part));
        }
      case 'tool.running':
        updateToolStatus(sessionId, asString(event.data['partId']) ?? '',
            ToolStatus.running, event.data);
      case 'tool.completed':
        updateToolStatus(sessionId, asString(event.data['partId']) ?? '',
            ToolStatus.completed, event.data);
      case 'tool.error':
        updateToolStatus(sessionId, asString(event.data['partId']) ?? '',
            ToolStatus.error, event.data);
      case 'session.status':
        setStatus(sessionId,
            sessionStatusFrom(asString(event.data['status'])));
      case 'session.finalizing':
        setStatus(sessionId, SessionStatus.finalizing);
      case 'session.error':
        setStatus(sessionId, SessionStatus.error);
    }
  }

  /// Snapshot refetch landed — merge without moving the UI backward
  /// (web `mergeSnapshotMessages`, commit dc1ce84).
  void setMessages(String sessionId, List<ChatMessage> snapshot) {
    final live = state.messagesOf(sessionId);
    final liveById = {for (final m in live) m.id: m};
    final liveByCmid = {
      for (final m in live)
        if (m.clientMessageId != null) m.clientMessageId!: m,
    };
    final used = <String>{};
    final merged = <ChatMessage>[];
    for (final snap in snapshot) {
      final liveMsg = liveById[snap.id] ??
          (snap.clientMessageId != null ? liveByCmid[snap.clientMessageId] : null);
      if (liveMsg == null) {
        merged.add(snap);
      } else {
        used.add(liveMsg.id);
        merged.add(_mergeMessage(liveMsg, snap));
      }
    }
    for (final m in live) {
      if (!used.contains(m.id)) merged.add(m);
    }
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
      parts.add(_mergePart(livePart, snapPart));
    }
    for (final p in live.parts) {
      if (!usedParts.contains(p.id)) parts.add(p);
    }
    return snap.copyWith(parts: parts);
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
            (m) => m.id == 'tmp-$cmid' || (m.id != message.id && m.clientMessageId == cmid),
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
      String sessionId, String messageId, String partId, String delta) {
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
  void updateToolStatus(String sessionId, String partId, ToolStatus status,
      Map<String, dynamic> data) {
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
    state = ChatStreamState(
      messages: state.messages,
      status: {...state.status, sessionId: status},
    );
  }

  void clearMessages(String sessionId) {
    final messages = Map<String, List<ChatMessage>>.of(state.messages)
      ..remove(sessionId);
    state = ChatStreamState(messages: messages, status: state.status);
  }

  void _patchMessage(
      String sessionId, String messageId, ChatMessage Function(ChatMessage) fn) {
    final list = state.messagesOf(sessionId);
    final index = list.indexWhere((m) => m.id == messageId);
    if (index == -1) return;
    final next = List<ChatMessage>.of(list)..[index] = fn(list[index]);
    _setSessionMessages(sessionId, next);
  }

  void _setSessionMessages(String sessionId, List<ChatMessage> list) {
    state = ChatStreamState(
      messages: {...state.messages, sessionId: list},
      status: state.status,
    );
  }
}

final chatStreamProvider =
    NotifierProvider<ChatStreamStore, ChatStreamState>(ChatStreamStore.new);
