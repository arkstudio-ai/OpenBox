import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/events/bus.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/widgets/toast.dart';
import '../api/chat_api.dart';
import '../utils/content_view.dart';
import '../utils/turn_view.dart';

/// Long-press actions on an assistant turn — the mobile analog of the web
/// hover meta row (`chat:meta.*`): copy, like/dislike, regenerate, fork.
Future<void> showTurnActions(
  BuildContext context,
  WidgetRef ref, {
  required String sessionId,
  required AssistantTurnData turn,
  required void Function(String messageId) onRegenerate,
}) {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  final api = ref.read(chatApiProvider);
  // Copy and react address the answer, not the whole trace: the meta row
  // belongs to the message that produced the final prose (web AssistantMeta).
  final content = buildAssistantContentView(turn.messages, false);
  final messageId = content.finalMessageId ?? turn.lastMessageId;
  final reaction = turn.messages.last.reaction;

  return showModalBottomSheet<void>(
    context: context,
    builder: (sheetContext) => SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          _action(
            sheetContext,
            t,
            icon: Icons.copy_outlined,
            label: i18n.t('chat:meta.copyReply'),
            onTap: () async {
              await Clipboard.setData(
                  ClipboardData(text: content.finalText));
              ref.read(toastProvider.notifier).info(i18n.t('chat:meta.copied'));
            },
          ),
          _action(
            sheetContext,
            t,
            icon: reaction == 'up' ? Icons.thumb_up : Icons.thumb_up_outlined,
            label: i18n.t('chat:meta.likeReply'),
            onTap: () => api.setReaction(
                sessionId, messageId, reaction == 'up' ? null : 'up'),
          ),
          _action(
            sheetContext,
            t,
            icon: reaction == 'down'
                ? Icons.thumb_down
                : Icons.thumb_down_outlined,
            label: i18n.t('chat:meta.dislikeReply'),
            onTap: () => api.setReaction(
                sessionId, messageId, reaction == 'down' ? null : 'down'),
          ),
          _action(
            sheetContext,
            t,
            icon: Icons.refresh,
            label: i18n.t('chat:meta.regenerate'),
            onTap: () async => onRegenerate(messageId),
          ),
          _action(
            sheetContext,
            t,
            icon: Icons.call_split,
            label: i18n.t('chat:meta.forkMessage'),
            onTap: () async {
              final session = await api.fork(sessionId, messageId);
              ref.read(appEventBusProvider).emit('workspace.refresh');
              if (context.mounted && session.id.isNotEmpty) {
                context.go(Paths.chat(session.id));
              }
            },
          ),
        ],
      ),
    ),
  );
}

Widget _action(
  BuildContext sheetContext,
  BossipTokens t, {
  required IconData icon,
  required String label,
  required Future<void> Function() onTap,
}) {
  return ListTile(
    leading: Icon(icon, size: 20, color: t.n700),
    title: Text(label, style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
    onTap: () async {
      Navigator.pop(sheetContext);
      await onTap();
    },
  );
}
