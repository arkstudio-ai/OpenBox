import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/api/auth_store.dart';
import '../../../shared/events/app_lifecycle.dart';
import '../../workspace/state/active_workspace_store.dart';
import '../api/knowledge_api.dart';
import '../models/memory_models.dart';
import '../models/wiki_models.dart';

/// Who and where the knowledge page is read for (web `useMemoryScope`).
/// Everything below watches it, so nothing read in one account or workspace
/// is shown in another.
typedef KnowledgeScope = ({String userId, String? workspaceId});

final knowledgeScopeProvider = Provider<KnowledgeScope>(
  (ref) => (
    userId: ref.watch(authProvider.select((state) => state.userId)),
    workspaceId: ref.watch(
      activeWorkspaceProvider.select((value) => value.valueOrNull?.currentId),
    ),
  ),
);

/// A scope and a search: what one list is read for.
typedef KnowledgeQuery = ({String projectId, String query});

extension ScopedValue<T> on AsyncValue<T> {
  /// The data to show. A provider rebuilt because its scope changed keeps the
  /// old scope's data attached while it reloads; that must not be shown.
  T? get current => isReloading ? null : valueOrNull;
}

/// Re-reads a provider after a delay and whenever the app comes back on
/// screen (web `refetchInterval` + `refetchOnWindowFocus`). Nothing is read
/// while the app is in the background.
class _Poll {
  _Poll(this._ref) {
    _ref.onDispose(stop);
    _ref.listen<bool>(appVisibleProvider, (previous, visible) {
      if (_alive && visible && previous == false) _ref.invalidateSelf();
    });
  }

  final Ref<Object?> _ref;
  var _alive = true;
  Timer? _timer;

  void after(Duration delay) {
    if (!_alive) return;
    _timer?.cancel();
    _timer = Timer(delay, () {
      if (_alive && _ref.read(appVisibleProvider)) _ref.invalidateSelf();
    });
  }

  void stop() {
    _alive = false;
    _timer?.cancel();
  }
}

/// A server-paged list that keeps as many pages as were loaded when it
/// refreshes, and starts over when the scope changes.
abstract class _PagedNotifier<T, A>
    extends AutoDisposeFamilyAsyncNotifier<Paged<T>, A> {
  int _pages = 1;
  int _generation = 0;
  KnowledgeScope? _scope;

  Future<Paged<T>> fetch(KnowledgeApi api, A arg, int offset);

  Duration interval(Paged<T>? data);

  @override
  Future<Paged<T>> build(A arg) async {
    final scope = ref.watch(knowledgeScopeProvider);
    if (scope != _scope) {
      _scope = scope;
      _pages = 1;
    }
    _generation++;
    final api = ref.watch(knowledgeApiProvider);
    final poll = _Poll(ref);
    Paged<T>? result;
    try {
      final first = await fetch(api, arg, 0);
      final items = [...first.items];
      var page = first;
      for (var loaded = 1; page.hasMore && loaded < _pages; loaded++) {
        page = await fetch(api, arg, page.nextOffset!);
        items.addAll(page.items);
      }
      return result = Paged(
        items,
        nextOffset: page.nextOffset,
        cleanupPending: first.cleanupPending,
        fetchedAt: DateTime.now(),
      );
    } finally {
      poll.after(interval(result));
    }
  }

  Future<void> loadMore() async {
    final current = state.valueOrNull;
    if (current == null ||
        !current.hasMore ||
        current.loadingMore ||
        state.isLoading) {
      return;
    }
    final generation = _generation;
    state = AsyncData(current.copyWith(loadingMore: true));
    try {
      final page = await fetch(
        ref.read(knowledgeApiProvider),
        arg,
        current.nextOffset!,
      );
      if (generation != _generation) return;
      _pages++;
      state = AsyncData(
        current.copyWith(
          items: [...current.items, ...page.items],
          nextOffset: () => page.nextOffset,
          loadingMore: false,
        ),
      );
    } catch (error, stack) {
      if (generation != _generation) return;
      state = AsyncError<Paged<T>>(
        error,
        stack,
      ).copyWithPrevious(AsyncData(current.copyWith(loadingMore: false)));
    }
  }
}

class MemoryListNotifier extends _PagedNotifier<MemoryRecord, KnowledgeQuery> {
  @override
  Future<Paged<MemoryRecord>> fetch(
    KnowledgeApi api,
    KnowledgeQuery arg,
    int offset,
  ) => api.listMemories(
    projectId: arg.projectId,
    query: arg.query,
    offset: offset,
  );

  @override
  Duration interval(Paged<MemoryRecord>? data) => const Duration(seconds: 30);
}

/// Active memories for a scope and search, newest first, paged on the server
/// so no memory is ever out of reach.
final memoryListProvider = AsyncNotifierProvider.autoDispose
    .family<MemoryListNotifier, Paged<MemoryRecord>, KnowledgeQuery>(
      MemoryListNotifier.new,
    );

class WikiLibraryNotifier extends _PagedNotifier<WikiSummary, KnowledgeQuery> {
  @override
  Future<Paged<WikiSummary>> fetch(
    KnowledgeApi api,
    KnowledgeQuery arg,
    int offset,
  ) => api.library(projectId: arg.projectId, query: arg.query, offset: offset);

  @override
  Duration interval(Paged<WikiSummary>? data) => const Duration(seconds: 15);
}

/// Topic pages for a scope and search. The reader shares the unsearched one
/// to resolve `[[links]]` and related pages.
final wikiLibraryProvider = AsyncNotifierProvider.autoDispose
    .family<WikiLibraryNotifier, Paged<WikiSummary>, KnowledgeQuery>(
      WikiLibraryNotifier.new,
    );

class DocumentsNotifier extends _PagedNotifier<KnowledgeDocument, String> {
  @override
  Future<Paged<KnowledgeDocument>> fetch(
    KnowledgeApi api,
    String arg,
    int offset,
  ) => api.documents(projectId: arg, offset: offset);

  /// Briskly only while something is still being organized.
  @override
  Duration interval(Paged<KnowledgeDocument>? data) =>
      data?.items.any(
            (doc) => KnowledgeDocument.processing.contains(doc.status),
          ) ??
          false
      ? const Duration(seconds: 4)
      : const Duration(seconds: 30);
}

/// Uploaded files in a scope.
final knowledgeDocumentsProvider = AsyncNotifierProvider.autoDispose
    .family<DocumentsNotifier, Paged<KnowledgeDocument>, String>(
      DocumentsNotifier.new,
    );

final memoryProcessingProvider = FutureProvider.autoDispose
    .family<MemoryProcessing, String>((ref, projectId) async {
      ref.watch(knowledgeScopeProvider);
      final api = ref.watch(knowledgeApiProvider);
      final poll = _Poll(ref);
      MemoryProcessing? result;
      try {
        return result = await api.processing(projectId);
      } finally {
        poll.after(Duration(seconds: (result?.pending ?? 0) > 0 ? 4 : 30));
      }
    });

/// Which topics each memory belongs to.
final memoryGroupsProvider = FutureProvider.autoDispose
    .family<List<MemoryTopic>, String>((ref, projectId) async {
      ref.watch(knowledgeScopeProvider);
      final api = ref.watch(knowledgeApiProvider);
      final poll = _Poll(ref);
      try {
        return await api.memoryGroups(projectId);
      } finally {
        poll.after(const Duration(seconds: 30));
      }
    });

final knowledgeProjectsProvider =
    FutureProvider.autoDispose<List<KnowledgeProject>>((ref) {
      ref.watch(knowledgeScopeProvider);
      return ref.watch(knowledgeApiProvider).projects();
    });

/// Whether this account may upload files to organize.
final knowledgeUploadsProvider = FutureProvider.autoDispose<bool>((ref) {
  ref.watch(knowledgeScopeProvider);
  return ref.watch(knowledgeApiProvider).uploadsEnabled();
});

final memorySettingsProvider = FutureProvider.autoDispose<MemorySettings>((
  ref,
) {
  ref.watch(knowledgeScopeProvider);
  return ref.watch(knowledgeApiProvider).settings();
});

// One memory, read fresh on every open: a list row or an earlier read never
// authorizes text.

final memoryDetailProvider = FutureProvider.autoDispose
    .family<MemoryRecord, String>((ref, id) {
      ref.watch(knowledgeScopeProvider);
      return ref.watch(knowledgeApiProvider).memory(id);
    });

final memorySourcesProvider = FutureProvider.autoDispose
    .family<List<MemorySource>, String>((ref, id) {
      ref.watch(knowledgeScopeProvider);
      return ref.watch(knowledgeApiProvider).sources(id);
    });

final memoryHistoryProvider = FutureProvider.autoDispose
    .family<List<MemoryRevision>, String>((ref, id) {
      ref.watch(knowledgeScopeProvider);
      return ref.watch(knowledgeApiProvider).history(id);
    });

final memoryCleanupProvider = FutureProvider.autoDispose
    .family<MemoryCleanup, String>((ref, id) {
      ref.watch(knowledgeScopeProvider);
      return ref.watch(knowledgeApiProvider).cleanup(id);
    });

/// A page that is gone stays gone: no polling once it answers 404.
bool isMissing(Object? error) =>
    error != null && apiErrorOf(error)?.status == 404;

/// One topic or document page, kept current while it is open.
final wikiPageProvider = FutureProvider.autoDispose.family<WikiPage, String>((
  ref,
  id,
) async {
  ref.watch(knowledgeScopeProvider);
  final api = ref.watch(knowledgeApiProvider);
  final poll = _Poll(ref);
  try {
    final page = await api.page(id);
    poll.after(const Duration(seconds: 10));
    return page;
  } catch (error) {
    isMissing(error) ? poll.stop() : poll.after(const Duration(seconds: 10));
    rethrow;
  }
});

/// After any write everything in the knowledge cache is read again (web
/// `invalidateQueries({ queryKey: key })`).
void refreshKnowledge(WidgetRef ref) => invalidateKnowledge(ref.invalidate);

/// The same, for a write that may outlive the widget that started it: pass
/// `ProviderScope.containerOf(context).invalidate`, taken before awaiting.
void invalidateKnowledge(void Function(ProviderOrFamily) invalidate) {
  for (final family in <ProviderOrFamily>[
    memoryListProvider,
    wikiLibraryProvider,
    knowledgeDocumentsProvider,
    memoryProcessingProvider,
    memoryGroupsProvider,
    memorySettingsProvider,
    memoryDetailProvider,
    memorySourcesProvider,
    memoryHistoryProvider,
    memoryCleanupProvider,
    wikiPageProvider,
  ]) {
    invalidate(family);
  }
}
