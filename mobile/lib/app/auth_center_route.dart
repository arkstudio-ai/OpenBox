import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/auth_center/auth_center_screen.dart';
import '../features/workspace/state/active_workspace_store.dart';
import '../shared/api/auth_store.dart';
import '../shared/models/workspace.dart';
import '../shared/router/paths.dart';

class AuthCenterRoute extends ConsumerWidget {
  const AuthCenterRoute({super.key, this.jobId});
  final String? jobId;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final userId = ref.watch(authProvider.select((state) => state.user?.id));
    final current = ref.watch(activeWorkspaceProvider).valueOrNull?.current;
    if (userId == null || current == null) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    final scope = (userId: userId, workspaceId: current.id);
    return AuthCenterScreen(
      key: ValueKey((scope, jobId, current.role)),
      scope: scope,
      canManage: current.role.canManage,
      initialJobId: jobId,
      onExit: () => context.canPop() ? context.pop() : context.go(Paths.app),
    );
  }
}
