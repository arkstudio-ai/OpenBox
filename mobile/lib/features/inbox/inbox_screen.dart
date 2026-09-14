import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/inbox.dart';
import '../../shared/widgets/section_tabs.dart';
import '../../shared/widgets/toast.dart';
import '../workspace/state/active_workspace_store.dart';
import 'api/inbox_api.dart';
import 'state/inbox_navigator.dart';
import 'widgets/inbox_item_tile.dart';

/// Message centre (web `paths.inbox`): tabs per category with unread counts,
/// a cursor-paged feed, pull to refresh, "mark all read", and taps that mark
/// the row read before routing through [InboxNavigator].
class InboxScreen extends ConsumerStatefulWidget {
  const InboxScreen({super.key, this.initialCategory});

  final String? initialCategory;

  @override
  ConsumerState<InboxScreen> createState() => _InboxScreenState();
}

class _InboxScreenState extends ConsumerState<InboxScreen> {
  late String _tab = inboxCategories.contains(widget.initialCategory)
      ? widget.initialCategory!
      : '';
  final _scroll = ScrollController();
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _scroll.addListener(_maybeLoadMore);
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  void _maybeLoadMore() {
    if (!_scroll.hasClients) return;
    if (_scroll.position.extentAfter < 320) {
      ref.read(inboxFeedProvider(_tab).notifier).loadMore();
    }
  }

  Future<void> _refresh() async {
    ref.invalidate(inboxFeedProvider(_tab));
    ref.invalidate(inboxUnreadProvider);
    await ref.read(inboxFeedProvider(_tab).future);
  }

  Future<void> _readAll() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await ref
          .read(inboxApiProvider)
          .readAll(category: _tab.isEmpty ? null : _tab);
      for (final category in ['', ...inboxCategories]) {
        ref.read(inboxFeedProvider(category).notifier).markAllReadLocally();
      }
      ref.invalidate(inboxUnreadProvider);
    } catch (_) {
      if (mounted) {
        ref
            .read(toastProvider.notifier)
            .error(ref.read(i18nProvider).t('inbox:loadFailed'));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _open(InboxItem item) async {
    final router = GoRouter.of(context);
    final navigator = InboxNavigator(ref.read, router);
    var link = item.link;
    for (final category in ['', ...inboxCategories]) {
      ref.read(inboxFeedProvider(category).notifier).markReadLocally(item.id);
    }
    if (item.unread) {
      try {
        // The read receipt is also the authoritative copy of the link.
        link = (await ref.read(inboxApiProvider).markRead(item.id)).link;
        ref.invalidate(inboxUnreadProvider);
      } catch (_) {
        // Offline or already gone: still try the link we have.
      }
    }
    if (!mounted) return;
    final result = await navigator.open(link, stillCurrent: () => mounted);
    if (result == InboxOpen.unavailable && mounted) {
      ref
          .read(toastProvider.notifier)
          .warning(ref.read(i18nProvider).t('inbox:unavailable'));
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final unread = ref.watch(inboxUnreadProvider).valueOrNull;
    final feed = ref.watch(inboxFeedProvider(_tab));
    final workspaces = ref.watch(activeWorkspaceProvider).valueOrNull;

    String label(String key, String category) {
      final count = unread?.forCategory(category.isEmpty ? null : category);
      final text = i18n.t('inbox:tabs.$key');
      return count != null && count > 0 ? '$text $count' : text;
    }

    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        titleSpacing: 0,
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              i18n.t('inbox:title'),
              style: TextStyle(
                fontSize: FontSizes.lg,
                fontWeight: FontWeight.w500,
                color: t.ink,
              ),
            ),
            Text(
              i18n.t('inbox:subtitle'),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
          ],
        ),
        actions: [
          TextButton(
            key: const ValueKey('inbox-read-all'),
            onPressed:
                _busy ||
                    (unread?.forCategory(_tab.isEmpty ? null : _tab) ?? 0) == 0
                ? null
                : _readAll,
            child: Text(
              i18n.t('inbox:readAll'),
              style: const TextStyle(fontSize: FontSizes.sm),
            ),
          ),
          const SizedBox(width: 4),
        ],
      ),
      body: Column(
        children: [
          SectionTabs(
            labels: {
              '': label('all', ''),
              for (final category in inboxCategories)
                category: label(category, category),
            },
            value: _tab,
            onChanged: (value) => setState(() => _tab = value),
          ),
          Expanded(
            child: RefreshIndicator(
              onRefresh: _refresh,
              child: feed.when(
                loading: () => const Center(
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
                error: (error, _) => _Message(
                  text: i18n.t('inbox:loadFailed'),
                  action: i18n.t('common:action.retry'),
                  onAction: () => ref.invalidate(inboxFeedProvider(_tab)),
                ),
                data: (state) => state.items.isEmpty
                    ? _Message(text: i18n.t('inbox:empty'))
                    : ListView.separated(
                        controller: _scroll,
                        physics: const AlwaysScrollableScrollPhysics(),
                        padding: const EdgeInsets.only(bottom: 24),
                        itemCount: state.items.length + (state.hasMore ? 1 : 0),
                        separatorBuilder: (_, _) =>
                            Divider(height: 1, color: t.hairSoft),
                        itemBuilder: (context, index) {
                          if (index >= state.items.length) {
                            return _MoreRow(
                              loading: state.loadingMore,
                              label: i18n.t('inbox:loadMore'),
                              onTap: () => ref
                                  .read(inboxFeedProvider(_tab).notifier)
                                  .loadMore(),
                            );
                          }
                          final item = state.items[index];
                          final foreign =
                              item.workspaceId != null &&
                              workspaces != null &&
                              item.workspaceId != workspaces.currentId;
                          return InboxItemTile(
                            item: item,
                            workspaceName: foreign
                                ? workspaces.items
                                          .where(
                                            (w) => w.id == item.workspaceId,
                                          )
                                          .map((w) => w.name)
                                          .firstOrNull ??
                                      item.workspaceId
                                : null,
                            onTap: () => _open(item),
                          );
                        },
                      ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _Message extends StatelessWidget {
  const _Message({required this.text, this.action, this.onAction});

  final String text;
  final String? action;
  final VoidCallback? onAction;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      children: [
        const SizedBox(height: 96),
        Center(
          child: Text(
            text,
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
          ),
        ),
        if (action != null)
          Center(
            child: TextButton(onPressed: onAction, child: Text(action!)),
          ),
      ],
    );
  }
}

class _MoreRow extends StatelessWidget {
  const _MoreRow({
    required this.loading,
    required this.label,
    required this.onTap,
  });

  final bool loading;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return SizedBox(
      height: 56,
      child: Center(
        child: loading
            ? const SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(strokeWidth: 2),
              )
            : TextButton(
                key: const ValueKey('inbox-load-more'),
                onPressed: onTap,
                child: Text(
                  label,
                  style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
                ),
              ),
      ),
    );
  }
}
