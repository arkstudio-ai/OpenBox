import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/message.dart';
import '../../shared/router/paths.dart';
import 'state/chat_session_controller.dart';
import 'state/pending_store.dart';
import 'state/stream_store.dart';
import 'utils/turn_view.dart';
import 'widgets/assistant_turn.dart';
import 'widgets/cards/permission_card.dart';
import 'widgets/cards/question_dock.dart';
import 'widgets/chat_flow.dart';
import 'widgets/composer/composer.dart';
import 'widgets/composer/resource_slot.dart';
import 'widgets/run_error_notice.dart';
import 'widgets/turn_actions_sheet.dart';
import 'widgets/typing_row.dart';
import 'widgets/user_bubble.dart';

/// Live chat pane for one session (web `ChatRoute`): flow + pending prompts
/// + composer. The screen chrome (app bar/drawer) lives in the app shell.
class ChatScreen extends ConsumerWidget {
  const ChatScreen({super.key, required this.sessionId, this.resources});

  final String sessionId;

  /// Resource centre, handed down by the app layer (§分层: features never
  /// import each other, the composition layer wires them together).
  final ComposerResourceSlot? resources;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final sessionState = ref.watch(chatSessionProvider(sessionId));
    final stream = ref.watch(chatStreamProvider);
    final pending = ref.watch(pendingProvider);
    final messages = stream.messagesOf(sessionId);
    final liveStatus = stream.statusOf(sessionId);
    final busy = isBusyStatus(liveStatus ?? sessionState.session?.status);
    final retry = stream.retryOf(sessionId);
    final runError = stream.runErrorOf(sessionId);

    final rows = buildChatRows(messages);
    final permissions = pending.permissionsOf(sessionId);
    final questions = pending.questionsOf(sessionId);

    final lastUserId = messages.lastWhere(
      (m) => m.isUser,
      orElse: () => const ChatMessage(id: '', sessionId: '', role: '', parts: []),
    ).id;

    // Only the conversation's newest task card takes edits (web parity).
    final lastTodoIndex = rows.lastIndexWhere(
      (r) => r is AssistantTurnData && r.todo != null,
    );

    final widgets = <Widget>[
      for (final (index, row) in rows.indexed)
        switch (row) {
          UserRowData(:final message) => UserBubble(message: message),
          AssistantTurnData() => GestureDetector(
              onLongPress: busy
                  ? null
                  : () => showTurnActions(
                        context,
                        ref,
                        sessionId: sessionId,
                        turn: row,
                        onRegenerate: (id) => ref
                            .read(chatSessionProvider(sessionId).notifier)
                            .regenerate(id),
                      ),
              child: AssistantTurn(
                turn: row,
                sessionId: sessionId,
                streaming: busy && index == rows.length - 1,
                retry: busy && index == rows.length - 1 ? retry : null,
                todoEditable: index == lastTodoIndex,
                onStop: busy && index == rows.length - 1
                    ? () => ref
                        .read(chatSessionProvider(sessionId).notifier)
                        .stop()
                    : null,
                onReview: () => context.push(Paths.workbench(sessionId, tab: 'review')),
                onRegenerate: (id) => ref
                    .read(chatSessionProvider(sessionId).notifier)
                    .regenerate(id),
                onDismiss: (id) => ref
                    .read(chatSessionProvider(sessionId).notifier)
                    .dismiss(id),
              ),
            ),
        },
      if (busy && (rows.isEmpty || rows.last is UserRowData))
        TypingRow(retry: retry),
      for (final permission in permissions)
        PermissionCard(request: permission),
      // At the end of the transcript, inside the scroller, because it reads as
      // the next turn in the conversation. It used to sit below the list as a
      // sibling of it, and a tall one — a segment approval carries three
      // scripts and their prompts — took the whole column: the list is an
      // Expanded, so it was free to shrink to nothing and the conversation
      // could not be scrolled at all while the run waited.
      for (final question in questions) QuestionDock(request: question),
    ];

    return Column(
      children: [
        Expanded(
          child: sessionState.loading && messages.isEmpty
              ? const Center(child: CircularProgressIndicator(strokeWidth: 2))
              : sessionState.failed && messages.isEmpty
                  ? _ErrorState(sessionId: sessionId)
                  : ChatFlow(rows: widgets, forceScrollToken: lastUserId),
        ),
        // One line, and it must survive until the next send, so it stays
        // above the composer rather than scrolling away with the transcript.
        if (runError != null)
          RunErrorNotice(
            message: runError,
            onDismiss: () =>
                ref.read(chatStreamProvider.notifier).clearRunError(sessionId),
          ),
        SafeArea(
          top: false,
          child: Composer(
            sessionKey: sessionId,
            session: sessionState.session,
            busy: busy,
            resources: resources,
            onSend: (text, attachments) => ref
                .read(chatSessionProvider(sessionId).notifier)
                .send(text, attachments: attachments),
            onStop: busy
                ? () => ref.read(chatSessionProvider(sessionId).notifier).stop()
                : null,
          ),
        ),
      ],
    );
  }
}

/// Snapshot fetch failed with nothing cached — error text + retry
/// (was a silent blank screen when the backend was unreachable).
class _ErrorState extends ConsumerWidget {
  const _ErrorState({required this.sessionId});

  final String sessionId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(
            i18n.t('common:state.error'),
            style: TextStyle(fontSize: FontSizes.base, color: t.n700),
          ),
          const SizedBox(height: 4),
          Text(
            i18n.t('errors:network'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n500),
          ),
          const SizedBox(height: 14),
          OutlinedButton(
            onPressed: () =>
                ref.read(chatSessionProvider(sessionId).notifier).reload(),
            style: OutlinedButton.styleFrom(
              side: BorderSide(color: t.hair),
              foregroundColor: t.ink,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(Radii.full),
              ),
            ),
            child: Text(i18n.t('common:action.retry'),
                style: const TextStyle(fontSize: FontSizes.sm)),
          ),
        ],
      ),
    );
  }
}
