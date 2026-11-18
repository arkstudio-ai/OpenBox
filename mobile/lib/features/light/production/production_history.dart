import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/utils/error_text.dart';
import 'production_api.dart';
import 'production_media.dart';
import 'production_stages.dart';

class ProductionStartPage extends ConsumerStatefulWidget {
  const ProductionStartPage({super.key, required this.kind, this.brief});

  final ProductionKind kind;
  final String? brief;

  @override
  ConsumerState<ProductionStartPage> createState() =>
      _ProductionStartPageState();
}

class _ProductionStartPageState extends ConsumerState<ProductionStartPage> {
  Object? _error;

  @override
  void initState() {
    super.initState();
    unawaited(_start());
  }

  Future<void> _start() async {
    setState(() => _error = null);
    try {
      final task = await ref
          .read(lightProductionApiProvider)
          .create(widget.kind, brief: widget.brief);
      if (mounted) {
        context.pushReplacement(
          Paths.lightProduction(task.sessionId),
          extra: task,
        );
      }
    } catch (error) {
      if (mounted) setState(() => _error = error);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    return Scaffold(
      appBar: AppBar(title: Text(widget.kind.label(i18n))),
      body: ProductionLoading(error: _error, onRetry: _start),
    );
  }
}

class ProductionHistoryPage extends ConsumerStatefulWidget {
  const ProductionHistoryPage({super.key});

  @override
  ConsumerState<ProductionHistoryPage> createState() =>
      _ProductionHistoryPageState();
}

class _ProductionHistoryPageState extends ConsumerState<ProductionHistoryPage> {
  final _items = <LightProduction>[];
  ProductionKind? _filter;
  Object? _error;
  bool _loading = false;
  bool _hasMore = false;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load({bool append = false}) async {
    if (_loading) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final page = await ref
          .read(lightProductionApiProvider)
          .history(offset: append ? _items.length : 0);
      if (!mounted) return;
      setState(() {
        if (!append) _items.clear();
        final known = _items.map((item) => item.sessionId).toSet();
        _items.addAll(page.items.where((item) => known.add(item.sessionId)));
        _hasMore = page.hasMore;
      });
    } catch (error) {
      if (mounted) setState(() => _error = error);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _open(LightProduction task) async {
    if (task.legacy) {
      await context.push(Paths.lightChat(task.sessionId));
    } else {
      await context.push(Paths.lightProduction(task.sessionId), extra: task);
    }
    if (mounted) await _load();
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final filtered = _items
        .where((item) => _filter == null || _filter == item.kind)
        .toList();
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        title: Text(i18n.t('light:production.history')),
        centerTitle: false,
      ),
      body: SafeArea(
        top: false,
        child: Column(
          children: [
            DefaultTabController(
              length: ProductionKind.values.length + 1,
              child: TabBar(
                isScrollable: true,
                tabAlignment: TabAlignment.start,
                onTap: (index) => setState(
                  () => _filter = index == 0
                      ? null
                      : ProductionKind.values[index - 1],
                ),
                tabs: [
                  Tab(text: i18n.t('light:production.all')),
                  for (final kind in ProductionKind.values)
                    Tab(text: kind.label(i18n)),
                ],
              ),
            ),
            Expanded(
              child: RefreshIndicator(
                onRefresh: _load,
                child: ListView(
                  padding: const EdgeInsets.all(16),
                  physics: const AlwaysScrollableScrollPhysics(),
                  children: [
                    for (final task in filtered) ...[
                      _HistoryTile(task: task, onTap: () => _open(task)),
                      const SizedBox(height: 10),
                    ],
                    if (_loading)
                      const Padding(
                        padding: EdgeInsets.all(16),
                        child: LinearProgressIndicator(),
                      )
                    else if (_error != null)
                      Column(
                        children: [
                          Text(errorText(i18n, _error!)),
                          TextButton(
                            onPressed: _load,
                            child: Text(i18n.t('common:action.retry')),
                          ),
                        ],
                      )
                    else if (filtered.isEmpty)
                      Padding(
                        padding: const EdgeInsets.symmetric(vertical: 30),
                        child: Text(
                          i18n.t('light:production.emptyHistory'),
                          textAlign: TextAlign.center,
                          style: TextStyle(color: t.n800),
                        ),
                      ),
                    if (_hasMore && !_loading)
                      TextButton(
                        onPressed: () => _load(append: true),
                        child: Text(i18n.t('light:production.more')),
                      ),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _HistoryTile extends ConsumerWidget {
  const _HistoryTile({required this.task, required this.onTap});

  final LightProduction task;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final media = task.output ?? task.materials.firstOrNull;
    final title = task.legacy && task.title.isNotEmpty
        ? task.title
        : task.brief.isNotEmpty
        ? task.brief
        : [
            task.shop,
            task.kind.label(i18n),
          ].where((s) => s.isNotEmpty).join(' · ');
    final statusColor = switch (task.status) {
      'failed' || 'blocked' => t.dangerInk,
      'completed' || 'working' => t.s800,
      _ => t.a700,
    };
    return Material(
      color: t.card,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(16),
        side: BorderSide(color: t.hair),
      ),
      child: InkWell(
        key: Key('production-history-${task.sessionId}'),
        borderRadius: BorderRadius.circular(16),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(10),
          child: Row(
            children: [
              SizedBox(
                width: 62,
                height: 62,
                child: media != null
                    ? ProductionThumbnail(media: media)
                    : DecoratedBox(
                        decoration: BoxDecoration(
                          color: t.surface,
                          borderRadius: BorderRadius.circular(12),
                        ),
                        child: Icon(
                          task.kind == ProductionKind.menu
                              ? Icons.restaurant_menu
                              : Icons.movie_outlined,
                          color: t.n800,
                        ),
                      ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontWeight: FontWeight.w700),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      task.kind.label(i18n),
                      style: TextStyle(fontSize: 13, color: t.n800),
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 6),
              Text(
                i18n.t('light:production.status.${task.status}'),
                style: TextStyle(
                  color: statusColor,
                  fontWeight: FontWeight.w600,
                ),
              ),
              Icon(Icons.chevron_right, color: t.n700, size: 19),
            ],
          ),
        ),
      ),
    );
  }
}
