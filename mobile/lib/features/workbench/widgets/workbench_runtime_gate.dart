import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/i18n/i18n.dart';
import '../../chat/api/assistant_api.dart';
import '../api/private_browser_api.dart';

/// Metadata is read before mounting any ordinary surface, even a deep link.
/// Missing/denied metadata must not fall back to a native ticket or shared WS.
final workbenchPrivateSessionProvider = FutureProvider.autoDispose
    .family<bool, PrivateBrowserScope>((ref, scope) async {
      final cancel = CancelToken();
      ref.onDispose(cancel.cancel);
      final json = await ref
          .watch(privateBrowserApiProvider(scope))
          .session(cancel);
      if (json['id'] != scope.sessionId ||
          json['workspace_id'] != scope.workspaceId) {
        throw StateError('Workbench Session scope changed');
      }
      final private =
          json['kind'] == 'assistant' ||
          json['visibility'] == 'private' ||
          json['memory_policy'] == 'assistant_isolated' ||
          json['assistant_managed'] == true;
      if (private && json['user_id'] != scope.userId) {
        throw StateError('Private Session owner changed');
      }
      return private;
    });

class WorkbenchRuntimeGate extends ConsumerWidget {
  const WorkbenchRuntimeGate({
    super.key,
    required this.sessionId,
    required this.privateBuilder,
    required this.ordinaryBuilder,
    this.privateOnly = false,
  });
  final String sessionId;
  final Widget Function(PrivateBrowserScope scope) privateBuilder;
  final WidgetBuilder ordinaryBuilder;

  /// A known assistant entry can never fall back to ordinary/native tooling.
  final bool privateOnly;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    // The explicitly global, ordinary desktop route has no Session context.
    if (sessionId.isEmpty) {
      return privateOnly ? _unavailable(ref) : ordinaryBuilder(context);
    }
    // A new login may keep the same actor/workspace record while advancing the
    // mutable AuthSession revision. Subscribe to auth state so the old scope
    // is disposed immediately, rather than waiting for the next status poll.
    ref.watch(authProvider);
    final selected = ref.watch(assistantScopeProvider);
    if (selected == null) {
      return const Center(child: CircularProgressIndicator());
    }
    final scope = (
      userId: selected.userId,
      workspaceId: selected.workspaceId,
      sessionId: sessionId,
      authRevision: ref.watch(authSessionProvider).revision,
    );
    return ref
        .watch(workbenchPrivateSessionProvider(scope))
        .when(
          skipLoadingOnRefresh: false,
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (_, _) => Center(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  ref
                      .watch(i18nProvider)
                      .t('workbench:privateBrowser.unavailable'),
                ),
                TextButton(
                  onPressed: () =>
                      ref.invalidate(workbenchPrivateSessionProvider(scope)),
                  child: Text(
                    ref.watch(i18nProvider).t('workbench:privateBrowser.check'),
                  ),
                ),
              ],
            ),
          ),
          data: (private) => private
              ? KeyedSubtree(key: ValueKey(scope), child: privateBuilder(scope))
              : privateOnly
              ? _unavailable(ref)
              : KeyedSubtree(
                  key: ValueKey(scope),
                  child: ordinaryBuilder(context),
                ),
        );
  }

  Widget _unavailable(WidgetRef ref) => Center(
    child: Text(
      ref.watch(i18nProvider).t('workbench:privateBrowser.unavailable'),
    ),
  );
}
