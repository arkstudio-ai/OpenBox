import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/api/containers_api.dart';
import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/events/bus.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/widgets/toast.dart';
import 'api/chat_api.dart';
import 'state/chat_session_controller.dart';
import 'state/config_providers.dart';
import 'widgets/composer/composer.dart';
import 'widgets/composer/resource_slot.dart';
import 'widgets/empty_state.dart';

const draftSessionKey = 'draft';

/// Empty chat (web `EmptyChatRoute`): greeting + suggestions + composer.
/// The first send creates the session, then navigates into it
/// (web `useStartChat`).
class EmptyChatScreen extends ConsumerStatefulWidget {
  const EmptyChatScreen({
    super.key,
    this.projectId,
    this.projectName,
    this.resources,
  });

  final String? projectId;
  final String? projectName;

  /// Resource centre, handed down by the app layer.
  final ComposerResourceSlot? resources;

  @override
  ConsumerState<EmptyChatScreen> createState() => _EmptyChatScreenState();
}

class _EmptyChatScreenState extends ConsumerState<EmptyChatScreen> {
  Future<void> _startChat(String text,
      [List<String> attachments = const []]) async {
    if (text.trim().isEmpty && attachments.isEmpty) return;
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
      await ref
          .read(chatSessionProvider(session.id).notifier)
          .send(text, attachments: attachments);
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
    final sandbox = ref.watch(runningContainerProvider);
    final needsSandbox = sandbox.hasValue && sandbox.valueOrNull == null;
    return Column(
      children: [
        Expanded(
          child: ChatEmptyState(
            projectName: widget.projectName,
            onPick: _startChat,
          ),
        ),
        if (needsSandbox) const _SandboxCard(),
        SafeArea(
          top: false,
          child: Composer(
            sessionKey: draftSessionKey,
            busy: false,
            resources: widget.resources,
            onSend: _startChat,
          ),
        ),
      ],
    );
  }
}

/// Sandbox gate (web `workspace:sandbox`): files and commands run in an
/// isolated sandbox — offer to create one when none is running.
class _SandboxCard extends ConsumerStatefulWidget {
  const _SandboxCard();

  @override
  ConsumerState<_SandboxCard> createState() => _SandboxCardState();
}

class _SandboxCardState extends ConsumerState<_SandboxCard> {
  bool _creating = false;

  Future<void> _create() async {
    setState(() => _creating = true);
    try {
      await ref.read(containersApiProvider).create();
      ref.invalidate(runningContainerProvider);
    } catch (_) {
      if (mounted) {
        ref
            .read(toastProvider.notifier)
            .error(ref.read(i18nProvider).t('workspace:sandbox.failed'));
      }
    } finally {
      if (mounted) setState(() => _creating = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 0, 12, 6),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.xl),
        border: Border.all(color: t.hair),
      ),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  i18n.t('workspace:sandbox.title'),
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w600,
                    color: t.n800,
                  ),
                ),
                const SizedBox(height: 3),
                Text(
                  i18n.t('workspace:sandbox.body'),
                  style: TextStyle(
                      fontSize: FontSizes.xs, color: t.n600, height: 1.5),
                ),
              ],
            ),
          ),
          const SizedBox(width: 12),
          FilledButton(
            onPressed: _creating ? null : _create,
            style: FilledButton.styleFrom(
              backgroundColor: t.ink,
              foregroundColor: t.bg,
              padding:
                  const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(Radii.full),
              ),
            ),
            child: Text(
              _creating
                  ? i18n.t('workspace:sandbox.creating')
                  : i18n.t('workspace:sandbox.create'),
              style: const TextStyle(fontSize: FontSizes.sm),
            ),
          ),
        ],
      ),
    );
  }
}
