import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/events/bus.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/widgets/toast.dart';
import 'api/chat_api.dart';
import 'state/chat_session_controller.dart';
import 'state/config_providers.dart';
import 'widgets/composer/composer.dart';
import 'widgets/empty_state.dart';

const draftSessionKey = 'draft';

/// Empty chat (web `EmptyChatRoute`): greeting + suggestions + composer.
/// The first send creates the session, then navigates into it
/// (web `useStartChat`).
class EmptyChatScreen extends ConsumerStatefulWidget {
  const EmptyChatScreen({super.key, this.projectId, this.projectName});

  final String? projectId;
  final String? projectName;

  @override
  ConsumerState<EmptyChatScreen> createState() => _EmptyChatScreenState();
}

class _EmptyChatScreenState extends ConsumerState<EmptyChatScreen> {
  Future<void> _startChat(String text) async {
    if (text.trim().isEmpty) return;
    try {
      final model = ref.read(pickedModelProvider(draftSessionKey)) ?? '';
      final agent = ref.read(pickedAgentProvider(draftSessionKey)) ?? 'build';
      final session = await ref.read(chatApiProvider).createSession(
            projectId: widget.projectId,
            model: model,
            agent: agent,
          );
      // Carry the draft picks onto the real session.
      ref.read(pickedModelProvider(session.id).notifier).state =
          model.isEmpty ? null : model;
      ref.read(pickedAgentProvider(session.id).notifier).state = agent;
      await ref.read(chatSessionProvider(session.id).notifier).send(text);
      ref.read(appEventBusProvider).emit('workspace.refresh');
      if (mounted) context.go(Paths.chat(session.id));
    } catch (e) {
      if (mounted) {
        ref
            .read(toastProvider.notifier)
            .error(errorText(ref.read(i18nProvider), e));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Expanded(
          child: ChatEmptyState(
            projectName: widget.projectName,
            onPick: _startChat,
          ),
        ),
        SafeArea(
          top: false,
          child: Composer(
            sessionKey: draftSessionKey,
            busy: false,
            onSend: _startChat,
          ),
        ),
      ],
    );
  }
}
