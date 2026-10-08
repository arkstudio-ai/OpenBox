import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../features/chat/api/assistant_api.dart';
import '../features/workspace/state/active_workspace_store.dart';
import '../shared/api/auth_store.dart';

final assistantScopeOverride = assistantScopeProvider.overrideWith((ref) {
  final user = ref.watch(authProvider).user?.id;
  final workspace = ref.watch(activeWorkspaceProvider).valueOrNull?.currentId;
  return user == null || workspace == null
      ? null
      : (userId: user, workspaceId: workspace);
});
