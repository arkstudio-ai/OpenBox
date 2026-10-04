import 'dart:async';
import 'dart:math';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/events/app_lifecycle.dart';
import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';
import '../api/assistant_api.dart';
import 'stream_store.dart';

typedef ExecutionReadScope = ({String sessionId, AssistantScope actor});

ChatMessage hiddenExecutionMessage(
  ChatMessage message, {
  String status = 'pending',
}) => ChatMessage(
  id: message.id,
  sessionId: message.sessionId,
  role: message.role,
  createdAt: message.createdAt,
  clientMessageId: message.clientMessageId,
  parts: const [],
  sourceStatus: status,
);

class ExecutionTranscriptState {
  const ExecutionTranscriptState({
    this.messages = const {},
    this.pending = true,
    this.failed = false,
  });
  final Map<String, ChatMessage> messages;
  final bool pending, failed;

  ChatMessage project(ChatMessage original) {
    if (original.id.startsWith('tmp-')) return original;
    if (failed) return hiddenExecutionMessage(original, status: 'unavailable');
    if (pending || !messages.containsKey(original.id)) {
      return hiddenExecutionMessage(original);
    }
    final checked = messages[original.id]!;
    return (original.sourceCheckedAt ?? '').compareTo(
              checked.sourceCheckedAt ?? '',
            ) >
            0
        ? original
        : checked;
  }
}

/// Independent of the ordinary streaming store: every retained page needs
/// current source evidence, including after foregrounding and before copying.
class ExecutionTranscript
    extends
        AutoDisposeFamilyNotifier<
          ExecutionTranscriptState,
          ExecutionReadScope
        > {
  bool _disposed = false, _again = false;
  int _epoch = 0;
  Future<void>? _reading;
  Timer? _poll;
  bool get _current =>
      !_disposed && ref.read(assistantScopeProvider) == arg.actor;

  @override
  ExecutionTranscriptState build(ExecutionReadScope scope) {
    _disposed = false;
    _epoch++;
    _reading = null;
    _again = false;
    ref.onDispose(() {
      _disposed = true;
      _poll?.cancel();
    });
    ref.listen(
      chatStreamProvider.select((s) => s.messagesOf(scope.sessionId)),
      (_, _) {
        unawaited(refresh());
      },
    );
    ref.listen(appVisibleProvider, (_, visible) {
      _epoch++;
      state = const ExecutionTranscriptState();
      if (visible) unawaited(refresh());
    });
    _poll = Timer.periodic(
      const Duration(seconds: 15),
      (_) => unawaited(refresh()),
    );
    Future.microtask(refresh);
    return const ExecutionTranscriptState();
  }

  Future<List<ChatMessage>> _read(List<String> ids) async {
    final api = ref.read(assistantApiProvider(arg.actor));
    final rows = <ChatMessage>[];
    for (var start = 0; start < ids.length; start += 100) {
      final selected = ids.sublist(start, min(start + 100, ids.length));
      final page = await api.messages(arg.sessionId, selected);
      if (!_current) throw StateError('Execution scope changed');
      if (page.length != selected.length ||
          page.map((m) => m.id).toSet().length != page.length ||
          page.any(
            (m) =>
                !selected.contains(m.id) ||
                m.sessionId != arg.sessionId ||
                m.sourceCheckedAt == null ||
                !const {
                  'available',
                  'pending',
                  'unavailable',
                }.contains(m.sourceStatus),
          )) {
        throw const FormatException('Missing current-source projection');
      }
      rows.addAll(page);
    }
    return rows;
  }

  void _apply(List<ChatMessage> rows) {
    final messages = {...state.messages};
    for (final next in rows) {
      final held = messages[next.id];
      if (held == null ||
          (held.sourceCheckedAt ?? '').compareTo(next.sourceCheckedAt ?? '') <=
              0) {
        messages[next.id] = next;
      }
    }
    final loaded = ref
        .read(chatStreamProvider)
        .messagesOf(arg.sessionId)
        .map((m) => m.id)
        .toSet();
    messages.removeWhere((id, _) => !loaded.contains(id));
    state = ExecutionTranscriptState(messages: messages, pending: false);
  }

  Future<void> refresh() {
    if (!_current || !ref.read(appVisibleProvider)) return Future.value();
    if (_reading != null) {
      _again = true;
      return _reading!;
    }
    late final Future<void> reading;
    reading = _refresh().whenComplete(() {
      if (!identical(_reading, reading)) return;
      _reading = null;
      if (_again && _current) {
        _again = false;
        unawaited(refresh());
      }
    });
    return _reading = reading;
  }

  Future<void> _refresh() async {
    final epoch = _epoch;
    final ids = ref
        .read(chatStreamProvider)
        .messagesOf(arg.sessionId)
        .where((m) => !m.id.startsWith('tmp-'))
        .map((m) => m.id)
        .toSet()
        .toList();
    try {
      final rows = await _read(ids);
      if (_current && epoch == _epoch) _apply(rows);
    } catch (_) {
      if (_current && epoch == _epoch) {
        _epoch++;
        state = const ExecutionTranscriptState(failed: true);
      }
    }
  }

  Future<bool> canCopy(List<String> ids, String text) async {
    if (!_current ||
        text.isEmpty ||
        ids.isEmpty ||
        !ref.read(appVisibleProvider)) {
      return false;
    }
    final epoch = _epoch;
    try {
      final rows = await _read(ids.toSet().toList());
      if (!_current || epoch != _epoch) return false;
      _apply(rows);
      final latest = [for (final row in rows) state.messages[row.id] ?? row];
      if (latest.any((m) => m.sourceStatus != 'available')) return false;
      bool contains(Object? value) => switch (value) {
        String() => value.contains(text),
        Map() => value.values.any(contains),
        List() => value.any(contains),
        _ => false,
      };
      return latest.any(
        (m) => m.parts.any(
          (p) => switch (p) {
            TextPart() => contains(p.text),
            ReasoningPart() => contains(p.text),
            ToolPart() => contains(p.output),
            _ => false,
          },
        ),
      );
    } catch (_) {
      if (_current) {
        _epoch++;
        state = const ExecutionTranscriptState(failed: true);
      }
      return false;
    }
  }
}

final executionTranscriptProvider = NotifierProvider.autoDispose
    .family<ExecutionTranscript, ExecutionTranscriptState, ExecutionReadScope>(
      ExecutionTranscript.new,
    );
