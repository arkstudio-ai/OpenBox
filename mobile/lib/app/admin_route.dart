import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/admin/admin_console.dart';
import '../features/admin/api/admin_api.dart';
import '../features/admin/widgets/admin_layout.dart';
import '../features/skills/api/skills_api.dart';
import '../features/workspace/state/active_workspace_store.dart';
import '../shared/api/auth_store.dart';
import '../shared/api/providers.dart';
import '../shared/i18n/i18n.dart';
import '../shared/router/paths.dart';

/// The entire nested navigator is removed on identity, workspace or role change,
/// including confirmation sheets and editors pushed above the console.
class AdminRoute extends ConsumerWidget {
  const AdminRoute({super.key, this.section = 'fleet'});
  final String section;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final auth = ref.watch(authProvider);
    ref.watch(activeWorkspaceProvider);
    final user = auth.user;
    if (auth.isLoading) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    if (user?.role != 'admin') {
      return Scaffold(
        body: Center(
          child: Text(ref.watch(i18nProvider).t('admin:mobile.forbidden')),
        ),
      );
    }
    final scope = (
      userId: user!.id,
      workspaceId: ref.read(workspaceScopeProvider).currentId,
    );
    return ProviderScope(
      key: ValueKey((scope, section)),
      overrides: [
        adminScopeProvider.overrideWithValue(scope),
        adminSkillsChangedProvider.overrideWithValue(() => bumpSkills(ref)),
      ],
      child: _AdminNavigator(
        section: section,
        onExit: () => context.canPop() ? context.pop() : context.go(Paths.app),
      ),
    );
  }
}

class _AdminNavigator extends StatefulWidget {
  const _AdminNavigator({required this.section, required this.onExit});
  final String section;
  final VoidCallback onExit;
  @override
  State<_AdminNavigator> createState() => _AdminNavigatorState();
}

class _AdminNavigatorState extends State<_AdminNavigator> {
  final _navigator = GlobalKey<NavigatorState>();
  @override
  Widget build(BuildContext context) => NavigatorPopHandler<Object?>(
    onPopWithResult: (result) => _navigator.currentState!.pop(result),
    child: AdminTheme(
      child: Navigator(
        key: _navigator,
        onGenerateRoute: (_) => MaterialPageRoute<void>(
          builder: (_) => AdminConsole(
            initialSection: widget.section,
            onExit: widget.onExit,
          ),
        ),
      ),
    ),
  );
}
