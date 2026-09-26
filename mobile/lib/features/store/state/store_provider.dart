import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/i18n/i18n.dart';
import '../../workspace/state/active_workspace_store.dart';
import '../api/store_api.dart';
import '../models/store.dart';

/// The active workspace's store. Rebuilds (and so re-fetches) whenever the
/// selected workspace changes; nothing is fetched before one is known.
class StoreController extends AsyncNotifier<StoreSnapshot> {
  @override
  Future<StoreSnapshot> build() async {
    final workspaceId = ref.watch(
      activeWorkspaceProvider.select((s) => s.valueOrNull?.currentId),
    );
    if (workspaceId == null) return const StoreSnapshot();
    return ref.watch(storeApiProvider).list();
  }

  Future<void> refresh() async {
    state = AsyncData(await ref.read(storeApiProvider).list());
  }

  /// `POST /api/stores`. A 409 (`STORE_EXISTS`, e.g. the web form won the
  /// race) is not a failure for the caller: the existing store is loaded.
  Future<Store> create({
    required String name,
    required String category,
    required List<String> mainPlatforms,
  }) async {
    final api = ref.read(storeApiProvider);
    Store store;
    try {
      store = await api.create(
        name: name,
        category: category,
        mainPlatforms: mainPlatforms,
      );
    } catch (error) {
      if (apiErrorOf(error)?.code != 'STORE_EXISTS') rethrow;
      final snapshot = await api.list();
      state = AsyncData(snapshot);
      final existing = snapshot.store;
      if (existing == null) rethrow;
      return existing;
    }
    state = AsyncData(
      (state.valueOrNull ?? const StoreSnapshot()).withStore(store),
    );
    return store;
  }

  Future<Store> patch(
    String id, {
    String? name,
    String? category,
    List<String>? mainPlatforms,
  }) async {
    final store = await ref
        .read(storeApiProvider)
        .patch(
          id,
          name: name,
          category: category,
          mainPlatforms: mainPlatforms,
        );
    state = AsyncData(
      (state.valueOrNull ?? const StoreSnapshot()).withStore(store),
    );
    return store;
  }
}

final storeProvider = AsyncNotifierProvider<StoreController, StoreSnapshot>(
  StoreController.new,
);

/// Store-aware starter cards in the current language (§2.4). Empty while
/// there is no store; consumers fall back to the locale cards on loading or
/// error.
final storeStarterCardsProvider = FutureProvider.autoDispose<List<StarterCard>>(
  (ref) async {
    final store = (await ref.watch(storeProvider.future)).store;
    if (store == null) return const [];
    final locale = ref.watch(i18nProvider.select((s) => s.language));
    return ref.watch(storeApiProvider).starterCards(store.id, locale: locale);
  },
);
