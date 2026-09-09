import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/models/interaction.dart';
import '../../../shared/models/json.dart';
import '../../../shared/ws/ws_client.dart';
import '../api/chat_api.dart';
import 'question_draft.dart';

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
  final _closed = <String>{};
  int _epoch = 0;
  int _refreshSequence = 0;

  @override
  PendingState build() {
    ref.watch(authProvider.select((s) => s.userId));
    _closed.clear();
    _epoch++;
    _sub?.cancel();
    _sub = ref.watch(wsClientProvider).events.listen(_onWsEvent);
    ref.onDispose(() {
      _epoch++;
      _sub?.cancel();
    });
    return const PendingState();
  }

  void _onWsEvent(WsEvent event) {
    switch (event.type) {
      case 'permission.asked':
        addPermission(PermissionRequest.fromJson(event.data));
      case 'permission.replied':
        removePermission(_requestId(event.data));
      case 'question.asked' || 'question.updated':
        addQuestion(QuestionRequest.fromJson(event.data));
      case 'question.replied' || 'question.rejected' || 'question.cancelled':
        removeQuestion(_requestId(event.data));
    }
  }

  /// The backend's replied/rejected events carry the request under `id`
  /// (`permission/permission.py`, `question/question.py`); older builds read
  /// `request_id` only, so an answered card never left the screen and every
  /// further tap was answered with 404.
  static String _requestId(Map<String, dynamic> data) =>
      asString(data['request_id']) ?? asString(data['id']) ?? '';

  /// Seed from `GET /api/agent/permission` + `/question` on session open.
  void seed(
    List<PermissionRequest> permissions,
    List<QuestionRequest> questions,
  ) {
    final permMap = <String, List<PermissionRequest>>{};
    for (final p in permissions) {
      permMap.putIfAbsent(p.sessionId, () => []).add(p);
    }
    final questionMap = <String, List<QuestionRequest>>{};
    for (final q in questions.where(
      (q) => !_closed.contains(q.id) && q.status == 'pending',
    )) {
      final existing = state
          .questionsOf(q.sessionId)
          .where((item) => item.id == q.id)
          .firstOrNull;
      final latest =
          existing != null && existing.draftRevision > q.draftRevision
          ? existing
          : q;
      questionMap.putIfAbsent(q.sessionId, () => []).add(latest);
      ref.read(questionDraftProvider.notifier).hydrate(latest);
    }
    state = PendingState(permissions: permMap, questions: questionMap);
    ref.read(questionDraftProvider.notifier).keepOnly({
      for (final list in questionMap.values)
        for (final q in list) q.id,
    });
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
    if (request.id.isEmpty ||
        _closed.contains(request.id) ||
        request.status != 'pending') {
      return;
    }
    final list = state.questionsOf(request.sessionId);
    final existing = list.where((q) => q.id == request.id).firstOrNull;
    if (existing != null && existing.draftRevision >= request.draftRevision) {
      return;
    }
    ref.read(questionDraftProvider.notifier).hydrate(request);
    state = PendingState(
      permissions: state.permissions,
      questions: {
        ...state.questions,
        request.sessionId: existing == null
            ? [...list, request]
            : [for (final q in list) q.id == request.id ? request : q],
      },
    );
  }

  void removeQuestion(String requestId) {
    if (requestId.isEmpty) return;
    _closed.add(requestId);
    if (_closed.length > 1000) _closed.remove(_closed.first);
    ref.read(questionDraftProvider.notifier).discard(requestId);
    state = PendingState(
      permissions: state.permissions,
      questions: {
        for (final entry in state.questions.entries)
          entry.key: entry.value.where((q) => q.id != requestId).toList(),
      },
    );
  }

  Future<void> refreshQuestions() => _refresh(includePermissions: false);

  Future<void> refreshAll() => _refresh(includePermissions: true);

  Future<void> _refresh({required bool includePermissions}) async {
    final epoch = _epoch;
    final sequence = ++_refreshSequence;
    try {
      final api = ref.read(chatApiProvider);
      final results = await Future.wait<dynamic>([
        api.listQuestions(),
        if (includePermissions) api.listPermissions(),
      ]);
      if (epoch != _epoch || sequence != _refreshSequence) return;
      seed(
        includePermissions
            ? results[1] as List<PermissionRequest>
            : state.permissions.values.expand((list) => list).toList(),
        results[0] as List<QuestionRequest>,
      );
    } catch (_) {
      /* A failed read must not discard pending cards or drafts. */
    }
  }
}

final pendingProvider = NotifierProvider<PendingStore, PendingState>(
  PendingStore.new,
);
