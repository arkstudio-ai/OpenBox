import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/auth/login_page.dart';
import '../features/auth/register_page.dart';
import '../features/billing/billing_screen.dart';
import '../features/chat/api/assistant_api.dart';
import '../features/chat/assistant_screen.dart';
import '../features/chat/chat_screen.dart';
import '../features/chat/empty_chat_screen.dart';
import '../features/chat/state/config_providers.dart';
import '../features/chat/widgets/assistant_tasks.dart';
import '../features/chat/widgets/composer/resource_slot.dart';
import '../features/cron/cron_screen.dart';
import '../features/inbox/inbox_screen.dart';
import '../features/inbox/topic_screen.dart';
import '../features/landing/landing_page.dart';
import '../features/legal/legal_page.dart';
import '../features/onboarding/state/onboarding_store.dart';
import '../features/onboarding/widgets/intro_banner_page.dart';
import '../features/resources/resources_screen.dart';
import '../features/resources/utils/upload_flow.dart';
import '../features/resources/widgets/resource_mention_section.dart';
import '../features/settings/settings_screen.dart';
import '../features/skills/skills_screen.dart';
import '../features/voice/voice_call_page.dart';
import '../features/voice/widgets/voice_call_button.dart';
import '../features/workbench/workbench_screen.dart';
import '../features/workbench/workbench_surface_page.dart';
import '../features/workspace/invite_screen.dart';
import '../features/workspace/state/active_workspace_store.dart';
import '../features/workspace/state/workspace_store.dart';
import '../shared/api/assistant_profile.dart';
import '../shared/api/auth_store.dart';
import '../shared/router/paths.dart';
import 'admin_route.dart';
import 'assistant_session_entry.dart';
import 'auth_center_route.dart';
import 'auth_redirect.dart';
import 'chat_location.dart';
import 'knowledge_routes.dart';
import 'workspace_shell.dart';

/// Route table (web `app/router/router.tsx` + guards). Mobile addition:
/// `/app/w/:sessionId` is the routed workbench panel.
class _AuthRefresh extends ChangeNotifier {
  _AuthRefresh(Ref ref) {
    ref.listen<AuthState>(authProvider, (_, _) => notifyListeners());
    ref.listen<AsyncValue<ActiveWorkspaceState>>(activeWorkspaceProvider, (
      before,
      after,
    ) {
      // A cold launch may be waiting behind the workspace error/retry gate.
      if (after.hasValue && before?.valueOrNull != after.valueOrNull) {
        notifyListeners();
      }
    });
  }
}

final routerProvider = Provider<GoRouter>((ref) {
  final refresh = _AuthRefresh(ref);
  ref.onDispose(refresh.dispose);
  return GoRouter(
    // Explicit deep links keep their destination. A normal launch restores
    // the account's last chat once its workspace membership is known.
    initialLocation: Paths.landing,
    refreshListenable: refresh,
    redirect: (context, state) async {
      final auth = ref.read(authProvider);
      final redirect = authRedirect(
        auth: auth,
        location: state.matchedLocation,
        uri: state.uri,
        introSeen: () => ref.read(introSeenProvider),
      );
      final returningHome =
          state.matchedLocation == Paths.landing ||
          state.matchedLocation == Paths.intro ||
          ((state.matchedLocation == Paths.login ||
                  state.matchedLocation == Paths.register) &&
              !state.uri.queryParameters.containsKey('redirect'));
      if (redirect != Paths.app || !returningHome || !auth.isAuthenticated) {
        return redirect;
      }
      if (!ref.read(chatLocationStoreProvider).hasSavedChat(auth.userId)) {
        return redirect;
      }
      try {
        final workspace = await ref.read(activeWorkspaceProvider.future);
        if (ref.read(authProvider).userId != auth.userId) return null;
        final workspaceId = workspace.currentId;
        if (workspaceId != null) {
          return ref.read(chatLocationStoreProvider).read((
                userId: auth.userId,
                workspaceId: workspaceId,
              )) ??
              redirect;
        }
      } catch (_) {
        // WorkspaceBootstrap owns the offline/error gate and its retry.
        return null;
      }
      return redirect;
    },
    routes: [
      GoRoute(
        path: Paths.legal,
        builder: (context, state) => const LegalPage(),
      ),
      GoRoute(
        path: '/legal/:document',
        builder: (context, state) =>
            LegalPage(document: state.pathParameters['document']),
        routes: [
          GoRoute(
            path: ':detail',
            builder: (context, state) =>
                LegalPage(document: state.pathParameters['detail']),
          ),
        ],
      ),
      GoRoute(
        path: Paths.intro,
        builder: (context, state) => const IntroBannerPage(),
      ),
      GoRoute(
        path: Paths.landing,
        builder: (context, state) => const LandingPage(),
      ),
      GoRoute(
        path: Paths.login,
        builder: (context, state) => const LoginPage(),
      ),
      GoRoute(
        path: Paths.register,
        builder: (context, state) => const RegisterPage(),
      ),
      GoRoute(
        path: '/invite/:token',
        builder: (context, state) =>
            InviteScreen(token: state.pathParameters['token'] ?? ''),
      ),
      GoRoute(
        path: Paths.app,
        builder: (context, state) => const _EmptyChatRoute(),
      ),
      GoRoute(
        path: Paths.assistant,
        builder: (context, state) => _AssistantRoute(
          taskId: state.uri.queryParameters['task'],
          resultId: state.uri.queryParameters['result'],
          intro: state.uri.queryParameters['intro'],
        ),
      ),
      // Full screen over everything (there is no shell route, so every
      // route here is on the root navigator). Popping it collapses the call
      // into the call bar; the call goes on.
      GoRoute(
        path: Paths.voice,
        pageBuilder: (context, state) => MaterialPage<void>(
          // Its own key per push: reopened from the call bar while the last
          // one still slides away, it must be a new page, not that one.
          key: state.pageKey,
          fullscreenDialog: true,
          child: const VoiceCallPage(),
        ),
      ),
      GoRoute(
        path: '/app/s/:sessionId',
        builder: (context, state) {
          final sessionId = state.pathParameters['sessionId']!;
          return WorkspaceShell(
            sessionId: sessionId,
            child: _ChatRoute(sessionId: sessionId),
          );
        },
      ),
      GoRoute(
        path: Paths.cron,
        builder: (context, state) => const CronScreen(),
      ),
      GoRoute(
        path: Paths.inbox,
        builder: (context, state) =>
            InboxScreen(initialCategory: state.uri.queryParameters['category']),
      ),
      GoRoute(
        path: '/app/topics/:slug',
        builder: (context, state) =>
            TopicScreen(slug: state.pathParameters['slug'] ?? ''),
      ),
      GoRoute(
        path: '/app/resources',
        builder: (context, state) => ResourcesScreen(
          initialProject: state.uri.queryParameters['project'],
        ),
      ),
      GoRoute(
        path: '/app/auth-center',
        builder: (context, state) =>
            AuthCenterRoute(jobId: state.uri.queryParameters['job']),
      ),
      GoRoute(
        path: Paths.skills,
        builder: (context, state) => const SkillsScreen(),
      ),
      ...knowledgeRoutes,
      GoRoute(
        path: Paths.admin,
        builder: (context, state) => const AdminRoute(),
      ),
      GoRoute(
        path: '/app/admin/:section',
        builder: (context, state) =>
            AdminRoute(section: state.pathParameters['section'] ?? 'fleet'),
      ),
      GoRoute(
        path: Paths.desktop,
        builder: (context, state) =>
            const WorkbenchSurfacePage(sessionId: '', kind: 'desktop'),
      ),
      GoRoute(
        path: '/app/billing',
        builder: (context, state) => const BillingScreen(),
        routes: [
          GoRoute(
            path: ':tab',
            builder: (context, state) => BillingScreen(
              initialTab: state.pathParameters['tab'] ?? 'purchase',
            ),
          ),
        ],
      ),
      GoRoute(
        path: '/app/settings',
        builder: (context, state) => SettingsScreen(
          initialTab: state.uri.queryParameters['tab'] ?? 'appearance',
        ),
      ),
      GoRoute(
        path: '/app/w/:sessionId',
        builder: (context, state) => WorkbenchScreen(
          sessionId: state.pathParameters['sessionId']!,
          initialTab:
              state.uri.queryParameters['tab'] ?? WorkbenchScreen.menuTab,
          initialControl: state.uri.queryParameters['control'] == '1',
        ),
      ),
    ],
  );
});

/// The composition layer's job (§分层): the resources feature owns the data,
/// the chat composer owns the menu that shows it, and they meet here — the
/// same hand-off the web does in `routes/workspace/*`.
ComposerResourceSlot composerResourceSlot(WidgetRef ref) =>
    ComposerResourceSlot(
      mentionSection:
          (context, {required query, required projectId, required onPick}) =>
              ResourceMentionSection(
                query: query,
                projectId: projectId,
                onPick: onPick,
              ),
      pickAndUpload: (context, {required projectId}) =>
          pickAndUploadResources(context, ref, projectId: projectId),
    );

class _ChatRoute extends ConsumerWidget {
  const _ChatRoute({required this.sessionId});

  final String sessionId;

  @override
  Widget build(BuildContext context, WidgetRef ref) => AssistantSessionEntry(
    sessionId: sessionId,
    builder: (_) => RememberChatLocation(
      location: Paths.chat(sessionId),
      child: ChatScreen(
        sessionId: sessionId,
        resources: composerResourceSlot(ref),
      ),
    ),
  );
}

class _AssistantRoute extends ConsumerWidget {
  const _AssistantRoute({this.taskId, this.resultId, this.intro});
  final String? taskId;
  final String? resultId;
  final String? intro;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final scope = ref.watch(assistantScopeProvider);
    final voiceEnabled =
        ref.watch(appConfigProvider).valueOrNull?.voiceEnabled ?? false;
    return WorkspaceShell(
      title: assistantLabel(ref),
      actions: [
        if (scope != null) VoiceCallButton(enabled: voiceEnabled),
        if (scope != null)
          AssistantTasksButton(key: ValueKey(scope), scope: scope),
      ],
      child: scope == null
          ? const Center(child: CircularProgressIndicator())
          : RememberChatLocation(
              location: Paths.assistant,
              child: AssistantScreen(
                key: ValueKey(scope),
                scope: scope,
                taskId: taskId,
                resultId: resultId,
                intro: intro,
                resources: composerResourceSlot(ref),
              ),
            ),
    );
  }
}

/// `/app` index (web `EmptyChatRoute`): the empty chat inside the shell,
/// scoped to the drawer's selected project.
class _EmptyChatRoute extends ConsumerWidget {
  const _EmptyChatRoute();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final projectId = ref.watch(selectedProjectProvider);
    final workspace = ref.watch(workspaceProvider).valueOrNull;
    final project = workspace?.projectById(projectId);
    return WorkspaceShell(
      child: RememberChatLocation(
        location: Paths.app,
        child: EmptyChatScreen(
          projectId: projectId,
          projectName: project?.name,
          resources: composerResourceSlot(ref),
        ),
      ),
    );
  }
}
