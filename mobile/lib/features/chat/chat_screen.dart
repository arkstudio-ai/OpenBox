import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

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
import 'widgets/typing_row.dart';
import 'widgets/user_bubble.dart';

/// Live chat pane for one session (web `ChatRoute`): flow + pending prompts
/// + composer. The screen chrome (app bar/drawer) lives in the app shell.
class ChatScreen extends ConsumerWidget {
  const ChatScreen({super.key, required this.sessionId});

  final String sessionId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final sessionState = ref.watch(chatSessionProvider(sessionId));
    final stream = ref.watch(chatStreamProvider);
    final pending = ref.watch(pendingProvider);
    final messages = stream.messagesOf(sessionId);
    final liveStatus = stream.statusOf(sessionId);
    final busy = isBusyStatus(liveStatus ?? sessionState.session?.status);

    final rows = buildChatRows(messages);
    final permissions = pending.permissionsOf(sessionId);
    final questions = pending.questionsOf(sessionId);

    final lastUserId = messages.lastWhere(
      (m) => m.isUser,
      orElse: () => const ChatMessage(id: '', sessionId: '', role: '', parts: []),
    ).id;

    final widgets = <Widget>[
      for (final (index, row) in rows.indexed)
        switch (row) {
          UserRowData(:final message) => UserBubble(message: message),
          AssistantTurnData() => AssistantTurn(
              turn: row,
              sessionId: sessionId,
              streaming: busy && index == rows.length - 1,
              onReview: () => context.push(Paths.workbench(sessionId)),
              onRegenerate: (id) => ref
                  .read(chatSessionProvider(sessionId).notifier)
                  .regenerate(id),
              onDismiss: (id) =>
                  ref.read(chatSessionProvider(sessionId).notifier).dismiss(id),
            ),
        },
      if (busy && (rows.isEmpty || rows.last is UserRowData)) const TypingRow(),
      for (final permission in permissions)
        PermissionCard(request: permission),
    ];

    return Column(
      children: [
        Expanded(
          child: sessionState.loading && messages.isEmpty
              ? const Center(child: CircularProgressIndicator(strokeWidth: 2))
              : ChatFlow(rows: widgets, forceScrollToken: lastUserId),
        ),
        for (final question in questions) QuestionDock(request: question),
        SafeArea(
          top: false,
          child: Composer(
            sessionKey: sessionId,
            session: sessionState.session,
            busy: busy,
            onSend: (text) =>
                ref.read(chatSessionProvider(sessionId).notifier).send(text),
            onStop: busy
                ? () => ref.read(chatSessionProvider(sessionId).notifier).stop()
                : null,
          ),
        ),
      ],
    );
  }
}
