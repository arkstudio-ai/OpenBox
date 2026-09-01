import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/models/container.dart';
import '../../../shared/models/diff.dart';
import '../../../shared/ws/ws_client.dart';
import '../api/workbench_api.dart';

/// Session diff, refreshed when the backend signals `session.diff`
/// (web `usePanelEvents` invalidation).
final sessionDiffProvider = FutureProvider.family<List<DiffEntry>, String>((
  ref,
  sessionId,
) {
  final sub = ref.watch(wsClientProvider).events.listen((event) {
    if (event.type == 'session.diff' && event.sessionId == sessionId) {
      ref.invalidateSelf();
    }
  });
  ref.onDispose(sub.cancel);
  return ref.watch(workbenchApiProvider).sessionDiff(sessionId);
});

/// The current session's exact project scope. One request feeds terminal,
/// files, cron, and menu labels so switching sessions cannot retain a stale
/// project directory.
final sessionWorkspaceProvider =
    FutureProvider.family<SessionWorkspace, String>((ref, sessionId) {
      return ref.watch(workbenchApiProvider).sessionWorkspace(sessionId);
    });

/// The session's project workdir (files-tab API root).
final sessionWorkdirProvider = FutureProvider.family<String?, String>((
  ref,
  sessionId,
) async {
  return (await ref.watch(
    sessionWorkspaceProvider(sessionId).future,
  )).directory;
});

/// The session's owning project (web `useCurrentProjectId`).
final sessionProjectIdProvider = FutureProvider.family<String?, String>((
  ref,
  sessionId,
) async {
  return (await ref.watch(
    sessionWorkspaceProvider(sessionId).future,
  )).projectId;
});

/// Directory listing for (containerId, path).
final fileListProvider =
    FutureProvider.family<List<FileEntry>, ({String containerId, String path})>(
      (ref, key) {
        return ref
            .watch(workbenchApiProvider)
            .listFiles(key.containerId, key.path);
      },
    );

/// File content for (containerId, path).
final fileContentProvider =
    FutureProvider.family<FileContent, ({String containerId, String path})>((
      ref,
      key,
    ) {
      return ref
          .watch(workbenchApiProvider)
          .fileContent(key.containerId, key.path);
    });

/// Keep in sync with backend truncation (5000 lines).
const fileContentLineLimit = 5000;
