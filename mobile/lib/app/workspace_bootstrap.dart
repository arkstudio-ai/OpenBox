import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../features/chat/state/config_providers.dart';
import '../features/workbench/widgets/desktop_activation_host.dart';
import '../features/workspace/state/active_workspace_store.dart';
import '../features/workspace/state/workspace_store.dart';
import '../shared/api/auth_store.dart';
import '../shared/appearance/tokens.dart';
import '../shared/i18n/i18n.dart';
import '../shared/models/workspace.dart';
import '../shared/utils/error_text.dart';
import '../shared/ws/ws_client.dart';

/// Holds authenticated UI until workspace scope is known, matching the web
/// WorkspaceLayout bootstrap. It also owns cross-feature cache reset whenever
/// the active tenant changes.
class WorkspaceBootstrap extends ConsumerWidget {
  const WorkspaceBootstrap({super.key, required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final auth = ref.watch(authProvider);
    if (!auth.isAuthenticated) return child;
    ref.listen(authProvider.select((value) => value.user?.id), (_, _) {
      ref.invalidate(pickedTeamProvider);
      ref.invalidate(pickedAgentProvider);
    });

    ref.listen<AsyncValue<ActiveWorkspaceState>>(activeWorkspaceProvider, (
      previous,
      next,
    ) {
      final before = previous?.valueOrNull?.currentId;
      final after = next.valueOrNull?.currentId;
      if (after == null || before == after) return;
      ref.invalidate(pickedTeamProvider);
      ref.invalidate(pickedAgentProvider);
      ref.read(selectedProjectProvider.notifier).state = null;
      ref.invalidate(workspaceProvider);
      if (before != null) {
        final ws = ref.read(wsClientProvider);
        ws.disconnect();
        unawaited(ws.connect());
      }
    });

    final workspaces = ref.watch(activeWorkspaceProvider);
    return workspaces.when(
      loading: () => const _WorkspaceLoading(),
      error: (error, _) => _WorkspaceError(
        message: errorText(ref.watch(i18nProvider), error),
        onRetry: () => ref.invalidate(activeWorkspaceProvider),
      ),
      data: (data) => data.currentId == null
          ? _WorkspaceError(
              message: ref.watch(i18nProvider).t('errors:WORKSPACE_FORBIDDEN'),
              onRetry: () => ref.invalidate(activeWorkspaceProvider),
            )
          : DesktopActivationHost(
              key: ValueKey((auth.userId, data.currentId)),
              scope: (userId: auth.userId, workspaceId: data.currentId!),
              workspaceName: data.current?.name ?? '',
              canManage: data.current?.role.canManage ?? false,
              child: child,
            ),
    );
  }
}

class _WorkspaceLoading extends StatelessWidget {
  const _WorkspaceLoading();

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: context.tokens.bg,
    body: const Center(child: CircularProgressIndicator(strokeWidth: 2)),
  );
}

class _WorkspaceError extends ConsumerWidget {
  const _WorkspaceError({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Scaffold(
      backgroundColor: t.bg,
      body: SafeArea(
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(Icons.cloud_off_outlined, color: t.n600, size: 32),
                const SizedBox(height: 12),
                Text(
                  message,
                  textAlign: TextAlign.center,
                  style: TextStyle(color: t.n700),
                ),
                const SizedBox(height: 16),
                FilledButton(
                  onPressed: onRetry,
                  child: Text(i18n.t('common:action.retry')),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
