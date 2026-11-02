import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/light.dart';
import '../../shared/router/paths.dart';
import '../chat/chat_screen.dart';
import '../chat/state/chat_session_controller.dart';
import '../chat/state/pending_store.dart';
import '../chat/state/stream_store.dart';
import '../chat/widgets/composer/resource_slot.dart';
import 'state/light_providers.dart';
import 'utils/light_steps.dart';
import 'widgets/light_input_bar.dart';
import 'widgets/light_result_actions.dart';
import 'widgets/step_bar.dart';

/// A topic chat (docs `LIGHT_MODE_PLAN.md` §4.4): the entry's step bar over
/// the ordinary conversation, the Light transcript skin, and the context
/// input bar instead of the composer. The conversation is a real session;
/// the pro chat opens the very same one with plain cards.
///
/// Steps light from the agent's cards (`detail.light.step_id`), never from a
/// guess at the transcript. The bar never goes back: the furthest step asked
/// stays lit, and while the agent works after an answer the next step shows
/// as in progress.
class LightTopicChatScreen extends ConsumerStatefulWidget {
  const LightTopicChatScreen({
    super.key,
    required this.sessionId,
    this.resources,
  });

  final String sessionId;
  final ComposerResourceSlot? resources;

  @override
  ConsumerState<LightTopicChatScreen> createState() =>
      _LightTopicChatScreenState();
}

class _LightTopicChatScreenState extends ConsumerState<LightTopicChatScreen> {
  /// Furthest step the agent has asked about (0-based), -1 before the first.
  int _reached = -1;

  void _back() => context.canPop() ? context.pop() : context.go(Paths.home);

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final sessionId = widget.sessionId;
    final start = ref.watch(lightStartsProvider)[sessionId];
    final sessionState = ref.watch(chatSessionProvider(sessionId));
    final entryId = start?.entry?.id ?? sessionState.session?.lightEntry;
    final LightEntry? entry =
        start?.entry ??
        (entryId == null ? null : ref.watch(lightEntryProvider(entryId)));
    final steps = start?.steps.isNotEmpty == true
        ? start!.steps
        : entry?.steps ?? const <LightStep>[];
    final chips = start?.openingCard.chips ?? entry?.openingCard.chips ?? [];

    final questions = ref.watch(
      pendingProvider.select((p) => p.questionsOf(sessionId)),
    );
    final asked = lightStepIndex(
      steps,
      questions.isEmpty ? null : lightStepIdOf(questions.last),
    );
    if (asked > _reached) _reached = asked;
    final delivered = lightResultDelivered(
      ref.watch(chatStreamProvider.select((s) => s.messagesOf(sessionId))),
    );
    if (delivered && steps.isNotEmpty) _reached = steps.length - 1;
    final liveStatus = ref.watch(
      chatStreamProvider.select((s) => s.statusOf(sessionId)),
    );
    final busy = isBusyStatus(liveStatus ?? sessionState.session?.status);
    final working =
        busy &&
            questions.isEmpty &&
            _reached >= 0 &&
            _reached < steps.length - 1
        ? _reached + 1
        : -1;

    final title = entry?.title ?? sessionState.session?.title ?? '';
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        leading: BackButton(
          key: const Key('light-chat-back'),
          onPressed: _back,
        ),
        titleSpacing: 0,
        title: Text(
          title.isEmpty ? i18n.t('light:conversations.untitled') : title,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w600,
            color: t.ink,
          ),
        ),
        bottom: steps.isEmpty
            ? null
            : PreferredSize(
                preferredSize: const Size.fromHeight(40),
                child: Align(
                  alignment: AlignmentDirectional.centerStart,
                  child: LightStepBar(
                    steps: steps,
                    activeIndex: _reached,
                    workingIndex: working,
                  ),
                ),
              ),
      ),
      body: ChatScreen(
        key: ValueKey('light-chat-$sessionId'),
        sessionId: sessionId,
        resources: widget.resources,
        light: true,
        lightResultActions: (context, group) =>
            lightResultActionsFor(sessionId, context, group),
        inputBuilder: (context, state) =>
            LightInputBar(state: state, chips: chips),
      ),
    );
  }
}
