import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/i18n/i18n.dart';
import '../../workspace/state/active_workspace_store.dart';
import '../api/store_api.dart';
import '../models/store.dart';

/// The active workspace's store; null until the owner has named one.
final storeProvider = FutureProvider<Store?>((ref) async {
  ref.watch(authProvider.select((a) => a.userId));
  final workspaceId = ref.watch(
    activeWorkspaceProvider.select((s) => s.valueOrNull?.currentId),
  );
  if (workspaceId == null) return null;
  return ref.watch(storeApiProvider).current();
});

/// 「今天可以做」 in the current language; empty without a store.
final storeStarterCardsProvider = FutureProvider<List<StarterCard>>((
  ref,
) async {
  final store = await ref.watch(storeProvider.future);
  if (store == null) return const [];
  final locale = ref.watch(i18nProvider.select((s) => s.language));
  return ref.watch(storeApiProvider).starterCards(store.id, locale: locale);
});
