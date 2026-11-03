import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/chat/api/assistant_api.dart';
import '../features/chat/assistant_screen.dart';
import '../features/chat/state/assistant_overview.dart';
import '../features/chat/state/config_providers.dart';
import '../features/chat/widgets/assistant_tasks.dart';
import '../features/chat/widgets/composer/resource_slot.dart';
import '../features/inbox/api/inbox_api.dart';
import '../features/inbox/inbox_screen.dart';
import '../features/light/autopilot_page.dart';
import '../features/light/conversations_tab.dart';
import '../features/light/home_screen.dart';
import '../features/light/me_screen.dart';
import '../features/light/topic_chat_screen.dart';
import '../features/light/topic_gallery_page.dart';
import '../features/light/widgets/light_shell.dart';
import '../features/voice/widgets/voice_call_button.dart';
import '../shared/api/assistant_profile.dart';
import '../shared/router/paths.dart';

typedef ResourceSlotOf = ComposerResourceSlot Function(WidgetRef ref);

/// Light 模式 routes (docs `LIGHT_MODE_IMPL_PLAN.md` §4.2 T2): the four-tab
/// shell. Composition only — each tab is a feature's own screen with the
/// app-level wiring the pro routes give it (assistant scope, voice button,
/// task sheet, resource slot). The pro route tree is untouched; `authRedirect`
/// sends an account to the home of its mode.
StatefulShellRoute lightShellRoute({required ResourceSlotOf resources}) =>
    StatefulShellRoute.indexedStack(
      builder: (context, state, shell) => _LightShellHost(shell: shell),
      branches: [
        StatefulShellBranch(
          routes: [
            GoRoute(
              path: Paths.home,
              builder: (context, state) => const LightHomeScreen(),
            ),
          ],
        ),
        StatefulShellBranch(
          routes: [
            GoRoute(
              path: Paths.lightAssistant,
              builder: (context, state) => _LightAssistantTab(
                taskId: state.uri.queryParameters['task'],
                resultId: state.uri.queryParameters['result'],
                intro: state.uri.queryParameters['intro'],
                resources: resources,
              ),
            ),
          ],
        ),
        StatefulShellBranch(
          routes: [
            GoRoute(
              path: Paths.lightInbox,
              builder: (context, state) => InboxScreen(
                embedded: true,
                initialCategory: state.uri.queryParameters['category'],
              ),
            ),
          ],
        ),
        StatefulShellBranch(
          routes: [
            GoRoute(
              path: Paths.me,
              builder: (context, state) => const LightMeScreen(),
            ),
          ],
        ),
      ],
    );

/// The topic pages above the tabs (root navigator, so they cover the bar):
/// an entry's gallery, and the guided conversation it starts.
List<GoRoute> lightRoutes({required ResourceSlotOf resources}) => [
  // Before the parameterised topic route: `/light/autopilot` is not a topic.
  GoRoute(
    path: Paths.lightAutopilot,
    builder: (context, state) => const LightAutopilotPage(),
  ),
  GoRoute(
    path: '/light/topic/:entryId',
    builder: (context, state) =>
        LightTopicGalleryPage(entryId: state.pathParameters['entryId'] ?? ''),
  ),
  GoRoute(
    path: '/light/s/:sessionId',
    builder: (context, state) => _LightChatRoute(
      sessionId: state.pathParameters['sessionId']!,
      resources: resources,
    ),
  ),
];

/// Badge counts for the bar come from the same reads the drawer makes.
class _LightShellHost extends ConsumerWidget {
  const _LightShellHost({required this.shell});

  final StatefulNavigationShell shell;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final scope = ref.watch(assistantScopeProvider);
    final assistantUnread = scope == null
        ? 0
        : ref
                  .watch(assistantOverviewProvider(scope))
                  .valueOrNull
                  ?.unreadCount ??
              0;
    final inboxUnread = ref.watch(inboxUnreadProvider).valueOrNull?.total ?? 0;
    return LightShell(
      shell: shell,
      inboxUnread: inboxUnread,
      assistantUnread: assistantUnread,
    );
  }
}

/// 「对话」: the personal assistant with its voice and task buttons, as the
/// pro `_AssistantRoute` wires it, minus the workspace shell.
class _LightAssistantTab extends ConsumerWidget {
  const _LightAssistantTab({
    required this.resources,
    this.taskId,
    this.resultId,
    this.intro,
  });

  final ResourceSlotOf resources;
  final String? taskId;
  final String? resultId;
  final String? intro;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final scope = ref.watch(assistantScopeProvider);
    final voiceEnabled =
        ref.watch(appConfigProvider).valueOrNull?.voiceEnabled ?? false;
    return LightConversationsTab(
      title: assistantLabel(ref),
      actions: [
        if (scope != null) VoiceCallButton(enabled: voiceEnabled),
        if (scope != null)
          AssistantTasksButton(key: ValueKey(scope), scope: scope),
      ],
      assistant: scope == null
          ? const Center(child: CircularProgressIndicator())
          : AssistantScreen(
              key: ValueKey(scope),
              scope: scope,
              taskId: taskId,
              resultId: resultId,
              intro: intro,
              embedded: true,
              resources: resources(ref),
            ),
    );
  }
}

class _LightChatRoute extends ConsumerWidget {
  const _LightChatRoute({required this.sessionId, required this.resources});

  final String sessionId;
  final ResourceSlotOf resources;

  @override
  Widget build(BuildContext context, WidgetRef ref) =>
      LightTopicChatScreen(sessionId: sessionId, resources: resources(ref));
}
