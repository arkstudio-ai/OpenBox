import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/team.dart';
import '../../../shared/utils/error_text.dart';
import '../api/teams_api.dart';

/// Paginated durable collections; new watermarks replace a page, never append
/// a second copy. Late responses from old filters or scopes are discarded.
class TeamCollection extends ConsumerStatefulWidget {
  const TeamCollection({
    super.key,
    required this.scope,
    required this.runId,
    required this.collection,
    required this.seq,
    required this.itemBuilder,
    this.query = const {},
    this.emptyLabel = 'teams:noTasks',
  });
  final TeamScope scope;
  final String runId, collection, emptyLabel;
  final int seq;
  final Map<String, String> query;
  final Widget Function(Map<String, dynamic>) itemBuilder;
  @override
  ConsumerState<TeamCollection> createState() => _TeamCollectionState();
}

class _TeamCollectionState extends ConsumerState<TeamCollection> {
  List<Map<String, dynamic>> _items = [];
  int? _next;
  int _epoch = 0;
  bool _busy = false;
  bool _failedReset = false;
  Object? _error;
  CancelToken? _cancel;
  @override
  void initState() {
    super.initState();
    unawaited(_load(reset: true));
  }

  @override
  void didUpdateWidget(TeamCollection oldWidget) {
    super.didUpdateWidget(oldWidget);
    final changedCollection =
        oldWidget.scope != widget.scope ||
        oldWidget.runId != widget.runId ||
        oldWidget.collection != widget.collection ||
        !mapEquals(oldWidget.query, widget.query);
    if (changedCollection) {
      _items = [];
      _next = null;
    }
    if (oldWidget.seq != widget.seq || changedCollection) {
      unawaited(_load(reset: true));
    }
  }

  @override
  void dispose() {
    _cancel?.cancel();
    super.dispose();
  }

  Future<void> _load({bool reset = false}) async {
    if (_busy && !reset) return;
    _cancel?.cancel();
    final cancel = _cancel = CancelToken();
    final epoch = ++_epoch;
    setState(() {
      _busy = true;
      _error = null;
      _failedReset = reset;
    });
    try {
      final page = await ref
          .read(teamsApiProvider)
          .read(
            widget.scope,
            '/api/team-runs/${Uri.encodeComponent(widget.runId)}/${widget.collection}',
            query: {...widget.query, 'offset': reset ? 0 : _next ?? 0},
            cancel: cancel,
          );
      if (!mounted || epoch != _epoch) return;
      final items = widget.collection == 'usage'
          ? [page]
          : asList(page['items']).map(asMap).toList();
      setState(() {
        _items = [if (!reset) ..._items, ...items];
        _next = asInt(page['next_offset']);
      });
    } catch (error) {
      if (mounted && epoch == _epoch) setState(() => _error = error);
    } finally {
      if (mounted && epoch == _epoch) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final item in _items) widget.itemBuilder(item),
        if (_busy) const LinearProgressIndicator(),
        if (_error != null)
          TextButton(
            onPressed: () => _load(reset: _failedReset),
            child: Text(errorText(i18n, _error!)),
          ),
        if (!_busy && _error == null && _items.isEmpty)
          Padding(
            padding: const EdgeInsets.all(16),
            child: Text(i18n.t(widget.emptyLabel)),
          ),
        if (_next != null)
          TextButton(
            onPressed: _busy ? null : _load,
            child: Text(i18n.t('teams:loadMore')),
          ),
      ],
    );
  }
}
