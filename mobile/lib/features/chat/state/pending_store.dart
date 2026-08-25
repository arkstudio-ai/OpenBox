import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/models/interaction.dart';
import '../../../shared/models/json.dart';
import '../../../shared/ws/ws_client.dart';

/// Pending permission/question requests grouped by session, mirroring
/// frontend-v2 `features/chat/stores/pending.ts` + its WS wiring.
class PendingState {
  const PendingState({this.permissions = const {}, this.questions = const {}});

  final Map<String, List<PermissionRequest>> permissions;
  final Map<String, List<QuestionRequest>> questions;

  List<PermissionRequest> permissionsOf(String sessionId) =>
      permissions[sessionId] ?? const [];

  List<QuestionRequest> questionsOf(String sessionId) =>
      questions[sessionId] ?? const [];
}

class PendingStore extends Notifier<PendingState> {
  StreamSubscription<WsEvent>? _sub;

  @override
  PendingState build() {
    _sub?.cancel();
    _sub = ref.watch(wsClientProvider).events.listen(_onWsEvent);
    ref.onDispose(() => _sub?.cancel());
    return const PendingState();
  }

  void _onWsEvent(WsEvent event) {
    switch (event.type) {
      case 'permission.asked':
        addPermission(PermissionRequest.fromJson(event.data));
      case 'permission.replied':
        removePermission(asString(event.data['request_id']) ?? '');
      case 'question.asked':
        addQuestion(QuestionRequest.fromJson(event.data));
      case 'question.replied' || 'question.rejected':
        removeQuestion(asString(event.data['request_id']) ?? '');
    }
  }

  /// Seed from `GET /api/agent/permission` + `/question` on session open.
  void seed(List<PermissionRequest> permissions, List<QuestionRequest> questions) {
    final permMap = <String, List<PermissionRequest>>{};
    for (final p in permissions) {
      permMap.putIfAbsent(p.sessionId, () => []).add(p);
    }
    final questionMap = <String, List<QuestionRequest>>{};
    for (final q in questions) {
      questionMap.putIfAbsent(q.sessionId, () => []).add(q);
    }
    state = PendingState(permissions: permMap, questions: questionMap);
  }

  void addPermission(PermissionRequest request) {
    if (request.id.isEmpty) return;
    final list = state.permissionsOf(request.sessionId);
    if (list.any((p) => p.id == request.id)) return;
    state = PendingState(
      permissions: {
        ...state.permissions,
        request.sessionId: [...list, request],
      },
      questions: state.questions,
    );
  }

  void removePermission(String requestId) {
    state = PendingState(
      permissions: {
        for (final entry in state.permissions.entries)
          entry.key: entry.value.where((p) => p.id != requestId).toList(),
      },
      questions: state.questions,
    );
  }

  void addQuestion(QuestionRequest request) {
    if (request.id.isEmpty) return;
    final list = state.questionsOf(request.sessionId);
    if (list.any((q) => q.id == request.id)) return;
    state = PendingState(
      permissions: state.permissions,
      questions: {
        ...state.questions,
        request.sessionId: [...list, request],
      },
    );
  }

  void removeQuestion(String requestId) {
    state = PendingState(
      permissions: state.permissions,
      questions: {
        for (final entry in state.questions.entries)
          entry.key: entry.value.where((q) => q.id != requestId).toList(),
      },
    );
  }
}

final pendingProvider =
    NotifierProvider<PendingStore, PendingState>(PendingStore.new);
