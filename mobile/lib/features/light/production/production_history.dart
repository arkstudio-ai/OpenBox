import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/utils/error_text.dart';
import '../utils/activity_date.dart';
import 'production_api.dart';
import 'production_media.dart';
import 'production_page.dart';
import 'production_stages.dart';
import 'production_widgets.dart';

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
  LightProduction? _preview;

  @override
  void initState() {
    super.initState();
    unawaited(_start());
  }

  Future<void> _start() async {
    setState(() => _error = null);
    try {
      final api = ref.read(lightProductionApiProvider);
      if (widget.kind == ProductionKind.video) {
        final task = await api.preview(widget.kind, brief: widget.brief);
        if (mounted) setState(() => _preview = task);
        return;
      }
      final task = await api.create(widget.kind, brief: widget.brief);
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
    if (_preview case final task?) {
      return ProductionPage(sessionId: task.sessionId, initial: task);
    }
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
  int _nextOffset = 0;
  int _request = 0;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load({bool append = false}) async {
    if (append && (_loading || !_hasMore)) return;
    final request = ++_request;
    final offset = append ? _nextOffset : 0;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final page = await ref
          .read(lightProductionApiProvider)
          .history(offset: offset, kind: _filter);
      if (!mounted || request != _request) return;
      setState(() {
        if (!append) _items.clear();
        final known = _items.map((item) => item.sessionId).toSet();
        _items.addAll(page.items.where((item) => known.add(item.sessionId)));
        _nextOffset = offset + page.items.length;
        _hasMore = page.hasMore;
      });
    } catch (error) {
      if (mounted && request == _request) setState(() => _error = error);
    } finally {
      if (mounted && request == _request) setState(() => _loading = false);
    }
  }

  void _select(int index) {
    final kind = index == 0 ? null : ProductionKind.values[index - 1];
    if (kind == _filter) return;
    _filter = kind;
    _items.clear();
    _hasMore = false;
    unawaited(_load());
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
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        titleSpacing: 0,
        title: Text(
          i18n.t('light:production.history'),
          style: TextStyle(
            color: t.ink,
            fontSize: FontSizes.xl2,
            fontWeight: FontWeight.w800,
          ),
        ),
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
                padding: const EdgeInsets.symmetric(horizontal: 8),
                labelColor: t.ink,
                unselectedLabelColor: t.n700,
                labelStyle: const TextStyle(
                  fontSize: FontSizes.base,
                  fontWeight: FontWeight.w700,
                ),
                unselectedLabelStyle: const TextStyle(fontSize: FontSizes.base),
                indicatorColor: t.a700,
                dividerColor: t.hair,
                onTap: _select,
                tabs: [
                  Tab(
                    key: const Key('production-filter-all'),
                    text: i18n.t('light:production.all'),
                  ),
                  for (final kind in ProductionKind.values)
                    Tab(
                      key: Key('production-filter-${kind.id}'),
                      text: kind.label(i18n),
                    ),
                ],
              ),
            ),
            Expanded(
              child: RefreshIndicator(
                onRefresh: _load,
                child: ListView(
                  key: ValueKey(_filter),
                  padding: const EdgeInsets.all(16),
                  physics: const AlwaysScrollableScrollPhysics(),
                  children: [
                    for (final task in _items) ...[
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
                    else if (_items.isEmpty)
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
    final title = [
      if (task.legacy) task.title,
      task.posterTitle,
      task.productName,
      task.brief,
      if (task.kind == ProductionKind.video &&
          task.shop.trim().isEmpty &&
          (task.referenceExampleId != null ||
              task.kind.templates.contains(task.template)))
        productionStyleLabel(i18n, task),
      [task.shop, task.kind.label(i18n)].where((s) => s.isNotEmpty).join(' · '),
    ].firstWhere((value) => value.trim().isNotEmpty);
    final statusColor = switch (task.status) {
      'failed' || 'blocked' => t.dangerInk,
      'completed' || 'working' => t.s800,
      _ => t.a700,
    };
    return Material(
      color: t.bg,
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
                width: 74,
                height: 60,
                child: media != null
                    ? ProductionThumbnail(media: media)
                    : DecoratedBox(
                        decoration: BoxDecoration(
                          color: t.surface,
                          borderRadius: BorderRadius.circular(12),
                        ),
                        child: Icon(switch (task.kind) {
                          ProductionKind.menu => Icons.restaurant_menu,
                          ProductionKind.poster => Icons.image_outlined,
                          _ => Icons.movie_outlined,
                        }, color: t.n800),
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
                      style: TextStyle(
                        fontSize: FontSizes.lg,
                        color: t.ink,
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      [
                        task.kind.label(i18n),
                        if (task.updatedAt case final date?)
                          lightActivityDate(date, i18n),
                      ].join(' · '),
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
                    ),
                    if (task.status != 'completed' && !task.legacy)
                      Text(
                        i18n.t('light:production.status.${task.status}'),
                        style: TextStyle(
                          color: statusColor,
                          fontSize: FontSizes.xs,
                        ),
                      ),
                  ],
                ),
              ),
              const SizedBox(width: 6),
              Icon(Icons.chevron_right, color: t.n700, size: 19),
            ],
          ),
        ),
      ),
    );
  }
}
