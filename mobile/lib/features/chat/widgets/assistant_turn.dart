import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/session.dart';
import '../utils/content_view.dart';
import '../utils/turn_view.dart';
import 'cards/inline_error_card.dart';
import 'cards/patch_chip.dart';
import 'cards/plan_card.dart';
import 'cards/todo_card.dart';
import 'cards/video_identity_card.dart';
import 'markdown_view.dart';
import 'result_artifacts.dart';
import 'step_divider.dart';
import 'traces/process_trace.dart';
import 'traces/thinking_trace.dart';
import 'traces/tool_chain_trace.dart';
import 'traces/work_log_trace.dart';
import 'typing_row.dart';

/// Assistant turn (web `AssistantTurn`): full column width, no avatar or
/// bubble; the collapsed trace rows stack on top, then a separately
/// identified final answer, then the artifacts it produced. Tool-step prose
/// stays in the work log rather than being concatenated into the answer.
class AssistantTurn extends ConsumerWidget {
  const AssistantTurn({
    super.key,
    required this.turn,
    required this.sessionId,
    required this.streaming,
    required this.onReview,
    required this.onRegenerate,
    required this.onDismiss,
    this.retry,
    this.onStop,
    this.todoEditable = false,
  });

  final AssistantTurnData turn;
  final String sessionId;

  /// This turn is the live one and the session is busy.
  final bool streaming;

  /// Set while a stalled run is retrying, so the wait can say which try.
  final RetryProgress? retry;

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
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final content = buildAssistantContentView(turn.messages, streaming);

    // "Thinking" is the state of having nothing yet — not of having no prose
    // yet. Once reasoning or a tool call has arrived the turn is visibly
    // working, and each of those blocks carries its own live heading, so a
    // second 正在思考中 underneath is both redundant and wrong.
    final hasActivity = content.hasFinal ||
        content.progress.isNotEmpty ||
        content.workEvents.isNotEmpty ||
        content.resultGroups.isNotEmpty ||
        content.verification != null ||
        turn.todo != null ||
        turn.hasThinking ||
        turn.hasTools;

    // Hold each trace live for its whole phase rather than deriving it from
    // per-part activity flags, which flip many times within one turn.
    final preAnswer = streaming && !content.hasFinal;
    final thinkingLive =
        preAnswer && (turn.thinkingStreaming || turn.hasThinking);
    final toolsLive = streaming && (turn.toolsStreaming || !content.hasFinal);
    final showFinalLabel = content.progress.isNotEmpty ||
        content.workEvents.isNotEmpty ||
        turn.hasTools ||
        turn.todo != null ||
        turn.hasThinking ||
        content.resultGroups.isNotEmpty;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (turn.hasProcess)
          ProcessTrace(
            turn: turn,
            active: preAnswer,
          ),
        if (turn.hasThinking)
          ThinkingTrace(
            turn: turn,
            active: thinkingLive,
          ),
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
        if (turn.hasTools)
          ToolChainTrace(
            turn: turn,
            active: toolsLive,
          ),
        WorkLogTrace(
          events: content.workEvents,
          active: preAnswer,
          defaultOpen: content.incomplete,
        ),
        VideoIdentityCards(
          parts: [for (final m in turn.messages) ...m.parts],
          sessionId: sessionId,
        ),
        if (streaming && !hasActivity)
          Align(alignment: Alignment.centerLeft, child: ThinkingRow(retry: retry))
        else if (content.hasFinal) ...[
          if (showFinalLabel)
            Padding(
              padding: const EdgeInsets.only(bottom: 3),
              child: Text(
                i18n.t('chat:final.title'),
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  fontWeight: FontWeight.w500,
                  color: t.n600,
                ),
              ),
            ),
          MarkdownView(content.finalText, streaming: streaming),
        ],
        if (content.incomplete && turn.error == null)
          const _IncompleteNotice(),
        if (turn.error != null && !streaming)
          InlineErrorCard(
            message: _errorMessage(i18n, turn.error!),
            onRegenerate: () => onRegenerate(turn.lastMessageId),
            onDismiss: () => onDismiss(turn.lastMessageId),
          ),
        ResultArtifacts(
          groups: content.resultGroups,
          verification: content.verification,
        ),
        for (final plan in turn.plans)
          PlanCard(plan: plan, sessionId: sessionId),
        for (final patch in turn.patches)
          PatchChip(patch: patch, onReview: onReview),
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

/// The run ended without ever producing an answer, but it did work — say so,
/// rather than leaving a turn that looks like it simply had nothing to add.
class _IncompleteNotice extends ConsumerWidget {
  const _IncompleteNotice();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Container(
      margin: const EdgeInsets.only(top: 6),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
      decoration: BoxDecoration(
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.md),
        color: t.n100.withValues(alpha: 0.5),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            i18n.t('chat:final.missingTitle'),
            style: TextStyle(
              fontSize: FontSizes.sm,
              fontWeight: FontWeight.w500,
              color: t.n700,
            ),
          ),
          const SizedBox(height: 2),
          Text(
            i18n.t('chat:final.missingBody'),
            style: TextStyle(
              fontSize: FontSizes.xs,
              height: 1.6,
              color: t.n600,
            ),
          ),
        ],
      ),
    );
  }
}
