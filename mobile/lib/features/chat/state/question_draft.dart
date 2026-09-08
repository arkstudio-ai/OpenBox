import 'package:flutter_riverpod/flutter_riverpod.dart';

/// What has been chosen so far on one pending question request, held outside
/// the card that shows it.
///
/// The card cannot own this. It renders at the end of the transcript, inside
/// a lazily built `ListView`, so its element is thrown away whenever the rows
/// above it change length — a turn arrives, the typing row appears, a
/// permission card comes and goes — and again whenever it scrolls out of the
/// viewport. Every one of those took the chosen options with it and left a
/// card whose 确认 was disabled again: tapping an option looked like it did
/// nothing. Web has no equivalent problem — its dock is keyed by request id
/// in a list that is never virtualised, so it simply stays mounted.
class QuestionDraft {
  const QuestionDraft({required this.picked, required this.custom});

  factory QuestionDraft.empty(int questions) => QuestionDraft(
    picked: List.generate(questions, (_) => const <String>{}),
    custom: List.filled(questions, ''),
  );

  /// Chosen option labels, one set per question.
  final List<Set<String>> picked;

  /// Typed answers, one per question.
  final List<String> custom;

  /// The chosen labels plus any typed answer, in the order asked — the shape
  /// `POST /api/agent/question/{id}` expects.
  List<String> answersAt(int index) {
    final typed = custom[index].trim();
    return [...picked[index], if (typed.isNotEmpty) typed];
  }

  List<List<String>> get answers =>
      List.generate(picked.length, answersAt, growable: false);

  bool get complete => answers.every((answers) => answers.isNotEmpty);

  QuestionDraft _copy({List<Set<String>>? picked, List<String>? custom}) =>
      QuestionDraft(
        picked: picked ?? this.picked,
        custom: custom ?? this.custom,
      );
}

/// Drafts by request id. Entries are dropped with the request itself
/// (`PendingStore.removeQuestion` / `seed`), so nothing outlives the card it
/// belongs to.
class QuestionDraftStore extends Notifier<Map<String, QuestionDraft>> {
  @override
  Map<String, QuestionDraft> build() => const {};

  QuestionDraft of(String requestId, int questions) {
    final draft = state[requestId];
    if (draft != null && draft.picked.length == questions) return draft;
    return QuestionDraft.empty(questions);
  }

  void toggle(
    String requestId,
    int questions,
    int index,
    String label, {
    required bool multiple,
    required bool on,
  }) {
    final draft = of(requestId, questions);
    final chosen = multiple ? {...draft.picked[index]} : <String>{};
    if (on) {
      chosen.add(label);
    } else {
      chosen.remove(label);
    }
    final picked = [...draft.picked]..[index] = chosen;
    state = {...state, requestId: draft._copy(picked: picked)};
  }

  void write(String requestId, int questions, int index, String text) {
    final draft = of(requestId, questions);
    if (draft.custom[index] == text) return;
    final custom = [...draft.custom]..[index] = text;
    state = {...state, requestId: draft._copy(custom: custom)};
  }

  void discard(String requestId) {
    if (!state.containsKey(requestId)) return;
    state = {
      for (final entry in state.entries)
        if (entry.key != requestId) entry.key: entry.value,
    };
  }

  /// Drop drafts for requests that are no longer pending.
  void keepOnly(Set<String> requestIds) {
    final next = {
      for (final entry in state.entries)
        if (requestIds.contains(entry.key)) entry.key: entry.value,
    };
    if (next.length != state.length) state = next;
  }
}

final questionDraftProvider =
    NotifierProvider<QuestionDraftStore, Map<String, QuestionDraft>>(
      QuestionDraftStore.new,
    );
