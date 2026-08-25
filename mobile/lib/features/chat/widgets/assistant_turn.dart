import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/i18n/i18n.dart';
import '../utils/turn_view.dart';
import 'attachment_gallery.dart';
import 'cards/inline_error_card.dart';
import 'cards/patch_chip.dart';
import 'cards/plan_card.dart';
import 'cards/todo_card.dart';
import 'markdown_view.dart';
import 'step_divider.dart';
import 'traces/process_trace.dart';
import 'traces/thinking_trace.dart';
import 'traces/tool_chain_trace.dart';

/// Assistant turn (web `AssistantTurn` / DEEIX layout): full column width,
/// no avatar/bubble; traces stacked ABOVE the prose in order
/// process → think → tools; artifacts and error below.
class AssistantTurn extends ConsumerWidget {
  const AssistantTurn({
    super.key,
    required this.turn,
    required this.sessionId,
    required this.streaming,
    required this.onReview,
    required this.onRegenerate,
    required this.onDismiss,
    this.onStop,
    this.todoEditable = false,
  });

  final AssistantTurnData turn;
  final String sessionId;

  /// This turn is the live one and the session is busy.
  final bool streaming;

  final VoidCallback onReview;
  final void Function(String messageId) onRegenerate;
  final void Function(String messageId) onDismiss;

  /// Abort the run — offered by the task card while one is in flight.
  final VoidCallback? onStop;

  /// This turn holds the conversation's newest task card, so its card is
  /// the one that may be edited.
  final bool todoEditable;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (turn.hasProcess)
          ProcessTrace(turn: turn, active: streaming && !turn.hasBody),
        if (turn.hasThinking) ThinkingTrace(turn: turn),
        // Web order: the task card sits between thinking and the (loose)
        // tool chain, above the prose.
        if (turn.todo != null)
          TodoCard(
            todo: turn.todo!,
            sessionId: sessionId,
            streaming: streaming,
            onStop: onStop,
            editable: todoEditable,
          ),
        if (turn.hasTools) ToolChainTrace(turn: turn),
        if (turn.hasBody)
          MarkdownView(
            turn.bodyText,
            streaming: streaming,
          ),
        for (final plan in turn.plans)
          PlanCard(plan: plan, sessionId: sessionId),
        for (final patch in turn.patches)
          PatchChip(patch: patch, onReview: onReview),
        if (turn.files.any(isGalleryMedia))
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 6),
            child: AttachmentGallery(
              parts: turn.files.where(isGalleryMedia).toList(),
            ),
          ),
        for (final file in turn.files.where((f) => !isGalleryMedia(f)))
          Align(
            alignment: Alignment.centerLeft,
            child: Padding(
              padding: const EdgeInsets.only(top: 6),
              child: FileChipRow(name: file.path.split('/').last),
            ),
          ),
        if (turn.error != null && !streaming)
          InlineErrorCard(
            message: _errorMessage(i18n, turn.error!),
            onRegenerate: () => onRegenerate(turn.lastMessageId),
            onDismiss: () => onDismiss(turn.lastMessageId),
          ),
        for (final notice in turn.notices) StepDivider(part: notice),
      ],
    );
  }

  String _errorMessage(I18nState i18n, Map<String, dynamic> error) {
    final message = error['message'];
    if (message is String && message.isNotEmpty) return message;
    return i18n.t('errors:fallback');
  }
}
