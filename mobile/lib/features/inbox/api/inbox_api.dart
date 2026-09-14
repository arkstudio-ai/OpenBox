import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/inbox.dart';
import '../../../shared/ws/ws_client.dart';

/// Message-centre transport + providers (docs/MESSAGE_CENTER.md): the unread
/// counts refresh on every `inbox.updated` WS event and on a slow poll; the
/// feed is cursor-paged per tab and marks rows read optimistically.
class InboxApi {
  InboxApi(this._dio);

  final Dio _dio;

  Future<InboxPage> list({
    String? category,
    String? cursor,
    int limit = 30,
    CancelToken? cancel,
  }) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/inbox',
      queryParameters: {
        'category': ?category,
        'cursor': ?cursor,
        'limit': limit,
      },
      cancelToken: cancel,
    );
    return InboxPage.fromJson(resp.data ?? const {});
  }

  Future<InboxUnread> unread() async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/inbox/unread');
    return InboxUnread.fromJson(resp.data ?? const {});
  }

  /// Idempotent; the response is the row itself, link included, so a push
  /// tap can route from the durable record rather than the payload.
  Future<InboxItem> markRead(String id, {CancelToken? cancel}) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/api/inbox/${Uri.encodeComponent(id)}/read',
      cancelToken: cancel,
    );
    return InboxItem.fromJson(resp.data ?? const {});
  }

  Future<int> readAll({String? category}) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/api/inbox/read-all',
      queryParameters: {'category': ?category},
    );
    return (resp.data?['updated'] as num?)?.toInt() ?? 0;
  }

  Future<TopicPage> topic(String slug, {CancelToken? cancel}) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/topics/${Uri.encodeComponent(slug)}',
      cancelToken: cancel,
    );
    return TopicPage.fromJson(resp.data ?? const {});
  }
}

final inboxApiProvider = Provider<InboxApi>(
  (ref) => InboxApi(ref.watch(apiDioProvider)),
);

const inboxUpdatedEvent = 'inbox.updated';

void _wireInboxInvalidation(Ref ref, {Duration? poll}) {
  final sub = ref.watch(wsClientProvider).events.listen((event) {
    if (event.type == inboxUpdatedEvent) ref.invalidateSelf();
  });
  ref.onDispose(sub.cancel);
  if (poll != null) {
    final timer = Timer.periodic(poll, (_) => ref.invalidateSelf());
    ref.onDispose(timer.cancel);
  }
}

/// Drawer badge. A push that arrives in the foreground also invalidates it
/// (`NotificationHost`), so the count moves even before the socket does.
final inboxUnreadProvider = FutureProvider<InboxUnread>((ref) {
  _wireInboxInvalidation(ref, poll: const Duration(minutes: 2));
  return ref.watch(inboxApiProvider).unread();
});

class InboxFeedState {
  const InboxFeedState({
    this.items = const [],
    this.nextCursor,
    this.loadingMore = false,
    this.moreError,
  });

  final List<InboxItem> items;
  final String? nextCursor;
  final bool loadingMore;
  final Object? moreError;

  bool get hasMore => nextCursor != null;

  InboxFeedState copyWith({
    List<InboxItem>? items,
    String? nextCursor,
    bool clearCursor = false,
    bool? loadingMore,
    Object? moreError,
    bool clearMoreError = false,
  }) => InboxFeedState(
    items: items ?? this.items,
    nextCursor: clearCursor ? null : (nextCursor ?? this.nextCursor),
    loadingMore: loadingMore ?? this.loadingMore,
    moreError: clearMoreError ? null : (moreError ?? this.moreError),
  );
}

/// One feed per tab (`''` = all). The first page reloads on `inbox.updated`;
/// later pages are appended on demand.
class InboxFeed extends FamilyAsyncNotifier<InboxFeedState, String> {
  @override
  Future<InboxFeedState> build(String arg) async {
    _wireInboxInvalidation(ref);
    final page = await ref
        .watch(inboxApiProvider)
        .list(category: arg.isEmpty ? null : arg);
    return InboxFeedState(items: page.items, nextCursor: page.nextCursor);
  }

  Future<void> loadMore() async {
    final current = state.valueOrNull;
    if (current == null || !current.hasMore || current.loadingMore) return;
    state = AsyncData(
      current.copyWith(loadingMore: true, clearMoreError: true),
    );
    try {
      final page = await ref
          .read(inboxApiProvider)
          .list(category: arg.isEmpty ? null : arg, cursor: current.nextCursor);
      final latest = state.valueOrNull ?? current;
      final known = {for (final item in latest.items) item.id};
      state = AsyncData(
        latest.copyWith(
          items: [
            ...latest.items,
            ...page.items.where((item) => !known.contains(item.id)),
          ],
          nextCursor: page.nextCursor,
          clearCursor: page.nextCursor == null,
          loadingMore: false,
        ),
      );
    } catch (error) {
      final latest = state.valueOrNull ?? current;
      state = AsyncData(latest.copyWith(loadingMore: false, moreError: error));
    }
  }

  /// Optimistic; the server call is the caller's (it also yields the link).
  void markReadLocally(String id) {
    final current = state.valueOrNull;
    if (current == null) return;
    state = AsyncData(
      current.copyWith(
        items: [
          for (final item in current.items)
            item.id == id && item.unread ? item.asRead() : item,
        ],
      ),
    );
  }

  void markAllReadLocally() {
    final current = state.valueOrNull;
    if (current == null) return;
    state = AsyncData(
      current.copyWith(
        items: [
          for (final item in current.items) item.unread ? item.asRead() : item,
        ],
      ),
    );
  }
}

final inboxFeedProvider =
    AsyncNotifierProvider.family<InboxFeed, InboxFeedState, String>(
      InboxFeed.new,
    );
