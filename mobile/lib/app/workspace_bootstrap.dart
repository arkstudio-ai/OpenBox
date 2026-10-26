import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../features/workbench/widgets/desktop_activation_host.dart';
import '../features/workspace/state/active_workspace_store.dart';
import '../features/workspace/state/workspace_store.dart';
import '../shared/api/auth_store.dart';
import '../shared/appearance/tokens.dart';
import '../shared/i18n/i18n.dart';
import '../shared/models/workspace.dart';
import '../shared/utils/error_text.dart';
import '../shared/ws/ws_client.dart';

final workspaceEventScopeProvider =
    Provider<({String userId, String workspaceId})?>((ref) {
      final auth = ref.watch(authProvider);
      if (!auth.isAuthenticated || auth.offline) return null;
      final workspace = ref
          .watch(activeWorkspaceProvider)
          .valueOrNull
          ?.currentId;
      return workspace == null
          ? null
          : (userId: auth.userId, workspaceId: workspace);
    });

/// Settings, knowledge and deep links need the same event connection as chat.
/// A scope change tears down the old handshake before opening the new one.
final workspaceEventsProvider = Provider.autoDispose<void>((ref) {
  final scope = ref.watch(workspaceEventScopeProvider);
  if (scope == null) return;
  final ws = ref.watch(wsClientProvider);
  unawaited(ws.connect());
  ref.onDispose(ws.disconnect);
});

/// Holds authenticated UI until workspace scope is known, matching the web
/// WorkspaceLayout bootstrap. It also owns cross-feature cache reset whenever
/// the active tenant changes.
///
/// Before that, it holds every screen while a saved sign-in could not be
/// checked at launch ([AuthState.offline]): the person is not signed out, so
/// neither the landing nor the login page is right. Retry settles it.
class WorkspaceBootstrap extends ConsumerWidget {
  const WorkspaceBootstrap({super.key, required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    ref.watch(workspaceEventsProvider);
    final auth = ref.watch(authProvider);
    if (auth.offline) {
      return auth.isLoading
          ? const _WorkspaceLoading()
          : _WorkspaceError(
              message: ref.watch(i18nProvider).t('auth:sessionOffline'),
              onRetry: () =>
                  unawaited(ref.read(authProvider.notifier).retryRestore()),
            );
    }
    if (!auth.isAuthenticated) return child;

    ref.listen<AsyncValue<ActiveWorkspaceState>>(activeWorkspaceProvider, (
      previous,
      next,
    ) {
      final before = previous?.valueOrNull?.currentId;
      final after = next.valueOrNull?.currentId;
      if (after == null || before == after) return;
      ref.read(selectedProjectProvider.notifier).state = null;
      ref.invalidate(workspaceProvider);
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
