import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/auth/login_page.dart';
import '../features/auth/register_page.dart';
import '../features/chat/chat_screen.dart';
import '../features/chat/empty_chat_screen.dart';
import '../features/landing/landing_page.dart';
import '../features/settings/settings_screen.dart';
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
            child: ChatScreen(sessionId: sessionId),
          );
        },
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
          initialTab: state.uri.queryParameters['tab'] ?? 'review',
        ),
      ),
    ],
  );
});

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
      ),
    );
  }
}
