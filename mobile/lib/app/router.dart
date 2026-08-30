import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/auth/login_page.dart';
import '../features/auth/register_page.dart';
import '../features/chat/chat_screen.dart';
import '../features/chat/empty_chat_screen.dart';
import '../features/chat/widgets/composer/resource_slot.dart';
import '../features/cron/cron_screen.dart';
import '../features/landing/landing_page.dart';
import '../features/resources/resources_screen.dart';
import '../features/resources/utils/upload_flow.dart';
import '../features/resources/widgets/resource_mention_section.dart';
import '../features/settings/settings_screen.dart';
import '../features/skills/skills_screen.dart';
import '../features/workbench/workbench_screen.dart';
import '../features/workspace/state/workspace_store.dart';
import '../shared/api/auth_store.dart';
import '../shared/router/paths.dart';
import 'workspace_shell.dart';

/// Route table (web `app/router/router.tsx` + guards). Mobile addition:
/// `/app/w/:sessionId` is the routed workbench panel.
class _AuthRefresh extends ChangeNotifier {
  _AuthRefresh(Ref ref) {
    ref.listen<AuthState>(authProvider, (_, _) => notifyListeners());
  }
}

final routerProvider = Provider<GoRouter>((ref) {
  final refresh = _AuthRefresh(ref);
  ref.onDispose(refresh.dispose);
  return GoRouter(
    initialLocation: Paths.landing,
    refreshListenable: refresh,
    redirect: (context, state) {
      final auth = ref.read(authProvider);
      final location = state.matchedLocation;
      if (auth.isLoading) return null;
      final inApp = location.startsWith(Paths.app);
      if (inApp && !auth.isAuthenticated) return Paths.login;
      final onAuthPage =
          location == Paths.login || location == Paths.register;
      if (onAuthPage && auth.isAuthenticated) return Paths.app;
      return null;
    },
    routes: [
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
        path: Paths.app,
        builder: (context, state) => const _EmptyChatRoute(),
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
        path: '/app/resources',
        builder: (context, state) => ResourcesScreen(
          initialProject: state.uri.queryParameters['project'],
        ),
      ),
      GoRoute(
        path: Paths.skills,
        builder: (context, state) => const SkillsScreen(),
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
        ),
      ),
    ],
  );
});

/// The composition layer's job (§分层): the resources feature owns the data,
/// the chat composer owns the menu that shows it, and they meet here — the
/// same hand-off the web does in `routes/workspace/*`.
ComposerResourceSlot _resourceSlot(WidgetRef ref) => ComposerResourceSlot(
      mentionSection: (context, {
        required query,
        required projectId,
        required onPick,
      }) =>
          ResourceMentionSection(
            query: query,
            projectId: projectId,
            onPick: onPick,
          ),
      pickAndUpload: (context, {required projectId}) =>
          pickAndUploadResources(ref, projectId: projectId),
    );

class _ChatRoute extends ConsumerWidget {
  const _ChatRoute({required this.sessionId});

  final String sessionId;

  @override
  Widget build(BuildContext context, WidgetRef ref) => ChatScreen(
        sessionId: sessionId,
        resources: _resourceSlot(ref),
      );
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
      child: EmptyChatScreen(
        projectId: projectId,
        projectName: project?.name,
        resources: _resourceSlot(ref),
      ),
    );
  }
}
