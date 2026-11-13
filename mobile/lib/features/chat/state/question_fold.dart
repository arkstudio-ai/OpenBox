import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/models/interaction.dart';

/// What this run of the app knows about one put-away question card.
class QuestionFold {
  const QuestionFold({required this.collapsed, this.expiresAt});

  /// Drawn as one row (true) or opened again by the person (false).
  final bool collapsed;

  /// The expiry the server set when it folded the card, if it said.
  final DateTime? expiresAt;
}

/// Whether a card may be put away at all: cards with a flow of their own
/// (registered server cards, desktop takeover, video approvals) are not.
bool questionFoldable(QuestionRequest request) =>
    request.questions.every((question) {
      final detail = question.detail;
      return detail == null ||
          (detail['kind'] == null &&
              detail['type'] == null &&
              !detail.containsKey('script_text') &&
              detail['segments'] is! List);
    });

/// Which question cards are put away ("稍后再说"), by request id.
///
/// Kept here rather than in the card: it sits in a lazy list and is rebuilt
/// from scratch whenever the rows above it change (see `questionDraftProvider`).
/// A card with no entry follows the server's `folded`, so one put away before
/// a reload comes back collapsed.
class QuestionFoldStore extends Notifier<Map<String, QuestionFold>> {
  @override
  Map<String, QuestionFold> build() => const {};

  /// The server folded the card: collapse it and remember its new expiry.
  void fold(String id, {DateTime? expiresAt}) => state = {
    ...state,
    id: QuestionFold(
      collapsed: true,
      expiresAt: expiresAt ?? state[id]?.expiresAt,
    ),
  };

  /// Open or close the row locally; the server is not told.
  void setCollapsed(String id, bool collapsed) => state = {
    ...state,
    id: QuestionFold(collapsed: collapsed, expiresAt: state[id]?.expiresAt),
  };

  void discard(String id) {
    if (!state.containsKey(id)) return;
    state = {
      for (final entry in state.entries)
        if (entry.key != id) entry.key: entry.value,
    };
  }
}

final questionFoldProvider =
    NotifierProvider<QuestionFoldStore, Map<String, QuestionFold>>(
      QuestionFoldStore.new,
    );
