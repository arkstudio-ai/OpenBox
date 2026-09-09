import 'dart:async';
import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/models/interaction.dart';
import '../api/chat_api.dart';
import 'pending_store.dart';

/// Switching to an option preserves typed text, but never submits both on a
/// single-choice question. The active answer is explicit, not a default.
class QuestionDraft {
  const QuestionDraft({
    required this.picked,
    required this.custom,
    required this.useCustom,
    this.revision = 0,
    this.dirty = false,
    this.saving = false,
    this.submitting = false,
    this.saveError,
    this.page = 0,
  });

  factory QuestionDraft.empty(int count) => QuestionDraft(
    picked: List.generate(count, (_) => <String>{}),
    custom: List.filled(count, ''),
    useCustom: List.filled(count, false),
  );

  factory QuestionDraft.fromAnswers(
    List<QuestionDraftAnswer> answers,
    int count,
    int revision,
  ) {
    final rows = List.generate(
      count,
      (i) => i < answers.length ? answers[i] : const QuestionDraftAnswer(),
    );
    final firstEmpty = rows.indexWhere(
      (row) => row.useCustom ? row.custom.trim().isEmpty : row.selected.isEmpty,
    );
    return QuestionDraft(
      picked: rows.map((r) => r.selected.toSet()).toList(),
      custom: rows.map((r) => r.custom).toList(),
      useCustom: rows.map((r) => r.useCustom).toList(),
      revision: revision,
      page: firstEmpty >= 0 ? firstEmpty : (count > 0 ? count - 1 : 0),
    );
  }

  final List<Set<String>> picked;
  final List<String> custom;
  final List<bool> useCustom;
  final int revision;
  final bool dirty;
  final bool saving;
  final bool submitting;
  final String? saveError;

  /// Local-only page position; never part of the answer or server revision.
  final int page;

  List<List<String>> get answers => List.generate(
    picked.length,
    (i) => useCustom[i]
        ? (custom[i].trim().isEmpty ? <String>[] : [custom[i].trim()])
        : picked[i].toList(),
  );
  bool get complete =>
      answers.isNotEmpty && answers.every((row) => row.isNotEmpty);
  List<QuestionDraftAnswer> get wire => List.generate(
    picked.length,
    (i) => QuestionDraftAnswer(
      selected: picked[i].toList(),
      custom: custom[i],
      useCustom: useCustom[i],
    ),
  );
  String get payload => jsonEncode(wire.map((item) => item.toJson()).toList());

  QuestionDraft copy({
    List<Set<String>>? picked,
    List<String>? custom,
    List<bool>? useCustom,
    int? revision,
    bool? dirty,
    bool? saving,
    bool? submitting,
    String? saveError,
    bool clearError = false,
    int? page,
  }) => QuestionDraft(
    picked: picked ?? this.picked,
    custom: custom ?? this.custom,
    useCustom: useCustom ?? this.useCustom,
    revision: revision ?? this.revision,
    dirty: dirty ?? this.dirty,
    saving: saving ?? this.saving,
    submitting: submitting ?? this.submitting,
    saveError: clearError ? null : saveError ?? this.saveError,
    page: page ?? this.page,
  );
}

/// Outside the virtualised card, scoped to the signed-in user, cached locally
/// immediately and saved with a server revision after a short debounce.
class QuestionDraftStore extends Notifier<Map<String, QuestionDraft>> {
  final _timers = <String, Timer>{};
  int _epoch = 0;
  String _userId = 'anonymous';

  @override
  Map<String, QuestionDraft> build() {
    _userId = ref.watch(authProvider.select((s) => s.userId));
    _epoch++;
    ref.onDispose(() {
      _epoch++;
      for (final timer in _timers.values) {
        timer.cancel();
      }
      _timers.clear();
    });
    return const {};
  }

  String key(String id) => 'openbox:question-draft:$_userId:$id';

  QuestionDraft of(String id, int count) =>
      state[id] ?? QuestionDraft.empty(count);

  void hydrate(QuestionRequest request) {
    final existing = state[request.id];
    if (existing != null && request.draftRevision <= existing.revision) return;
    if (existing != null &&
        (existing.dirty || existing.saving || existing.submitting)) {
      return;
    }
    var draft = QuestionDraft.fromAnswers(
      request.draft,
      request.questions.length,
      request.draftRevision,
    );
    if (existing == null) {
      try {
        final raw = ref.read(prefsProvider).getString(key(request.id));
        final cached = raw == null
            ? null
            : jsonDecode(raw) as Map<String, dynamic>;
        if (cached?['revision'] == request.draftRevision &&
            cached?['draft'] is List &&
            (cached!['draft'] as List).length == request.questions.length) {
          final answers = (cached['draft'] as List)
              .cast<Map<String, dynamic>>()
              .map(QuestionDraftAnswer.fromJson)
              .toList();
          final local = QuestionDraft.fromAnswers(
            answers,
            request.questions.length,
            request.draftRevision,
          );
          draft = local.copy(dirty: local.payload != draft.payload);
          final page = cached['page'];
          if (page is int && page >= 0 && page < request.questions.length) {
            draft = draft.copy(page: page);
          }
        }
      } catch (_) {
        /* A corrupt cache cannot hide a server draft. */
      }
    }
    if (existing != null) draft = draft.copy(page: existing.page);
    state = {...state, request.id: draft};
    if (draft.dirty) _schedule(request.id);
  }

  void _cache(String id, QuestionDraft draft) {
    unawaited(
      ref
          .read(prefsProvider)
          .setString(
            key(id),
            jsonEncode({
              'revision': draft.revision,
              'draft': draft.wire.map((r) => r.toJson()).toList(),
              'page': draft.page,
            }),
          )
          .catchError((Object _) => false),
    );
  }

  void _edit(String id, QuestionDraft draft) {
    if (draft.submitting) return;
    final next = draft.copy(dirty: true, clearError: true);
    state = {...state, id: next};
    _cache(id, next);
    _schedule(id);
  }

  void setPage(String id, int count, int page) {
    final draft = of(id, count);
    if (draft.submitting || page < 0 || page >= count || page == draft.page) {
      return;
    }
    final next = draft.copy(page: page);
    state = {...state, id: next};
    _cache(id, next);
  }

  void toggle(
    String id,
    int count,
    int index,
    String label, {
    required bool multiple,
    required bool on,
  }) {
    final draft = of(id, count);
    final chosen = multiple ? {...draft.picked[index]} : <String>{};
    on ? chosen.add(label) : chosen.remove(label);
    _edit(
      id,
      draft.copy(
        picked: [...draft.picked]..[index] = chosen,
        useCustom: [...draft.useCustom]..[index] = false,
      ),
    );
  }

  void write(String id, int count, int index, String text) {
    final draft = of(id, count);
    _edit(
      id,
      draft.copy(
        custom: [...draft.custom]..[index] = text,
        useCustom: [...draft.useCustom]..[index] = true,
      ),
    );
  }

  void setSubmitting(String id, bool value) {
    final draft = state[id];
    if (draft == null) return;
    state = {...state, id: draft.copy(submitting: value)};
    if (value) {
      _timers.remove(id)?.cancel();
    } else {
      _schedule(id);
    }
  }

  void _schedule(String id) {
    _timers.remove(id)?.cancel();
    final draft = state[id];
    if (draft == null ||
        !draft.dirty ||
        draft.saving ||
        draft.submitting ||
        draft.saveError != null) {
      return;
    }
    _timers[id] = Timer(
      const Duration(milliseconds: 400),
      () => unawaited(save(id)),
    );
  }

  Future<void> save(String id) async {
    final draft = state[id];
    if (draft == null || !draft.dirty || draft.saving || draft.submitting) {
      return;
    }
    final epoch = _epoch;
    state = {...state, id: draft.copy(saving: true, clearError: true)};
    try {
      final response = await ref
          .read(chatApiProvider)
          .saveQuestionDraft(id, draft.wire, draft.revision);
      if (epoch != _epoch || !state.containsKey(id)) return;
      final live = state[id]!;
      final saved = live.copy(
        revision: response.draftRevision,
        dirty: live.payload != draft.payload,
        saving: false,
      );
      state = {...state, id: saved};
      _cache(id, saved);
      _schedule(id);
    } catch (error) {
      if (epoch != _epoch || !state.containsKey(id)) return;
      final code = error is DioException ? error.response?.statusCode : null;
      if (code == 404 || code == 410) {
        ref.read(pendingProvider.notifier).removeQuestion(id);
      } else {
        state = {
          ...state,
          id: state[id]!.copy(
            saving: false,
            saveError: code == 409 ? 'conflict' : 'failed',
          ),
        };
      }
    }
  }

  Future<void> retry(String id) async {
    final epoch = _epoch;
    if (state[id]?.saveError == 'conflict') {
      try {
        final latest = await ref.read(chatApiProvider).getQuestion(id);
        if (epoch != _epoch || !state.containsKey(id)) return;
        if (latest.status != 'pending') {
          ref.read(pendingProvider.notifier).removeQuestion(id);
          return;
        }
        state = {...state, id: state[id]!.copy(revision: latest.draftRevision)};
      } catch (_) {
        return;
      }
    }
    await save(id);
  }

  void discard(String id) {
    _timers.remove(id)?.cancel();
    unawaited(
      ref.read(prefsProvider).remove(key(id)).catchError((Object _) => false),
    );
    state = {
      for (final e in state.entries)
        if (e.key != id) e.key: e.value,
    };
  }

  void keepOnly(Set<String> ids) {
    for (final id in state.keys.toList()) {
      if (!ids.contains(id)) discard(id);
    }
  }
}

final questionDraftProvider =
    NotifierProvider<QuestionDraftStore, Map<String, QuestionDraft>>(
      QuestionDraftStore.new,
    );
