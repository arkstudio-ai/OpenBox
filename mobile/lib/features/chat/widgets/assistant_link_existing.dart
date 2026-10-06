import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/utils/error_text.dart';
import '../api/assistant_api.dart';
import '../state/assistant_controller.dart';

/// The user's conversations the assistant could start following, newest
/// first (web `AssistantLinkExisting`). Read when shown; an untitled
/// conversation says so in words, never by its id.
class AssistantLinkExisting extends ConsumerStatefulWidget {
  const AssistantLinkExisting({super.key, required this.scope});
  final AssistantScope scope;
  @override
  ConsumerState<AssistantLinkExisting> createState() => _LinkState();
}

class _LinkState extends ConsumerState<AssistantLinkExisting> {
  final _items = <String, Map<String, dynamic>>{};
  String? _cursor, _sending, _linked;
  bool _loading = false;

  /// Reading the list failed / handing one over failed.
  Object? _error, _linkError;
  bool get _current =>
      mounted &&
      ref.read(authSessionProvider).userId == widget.scope.userId &&
      ref.read(workspaceScopeProvider).currentId == widget.scope.workspaceId;

  @override
  void initState() {
    super.initState();
    Future.microtask(_load);
  }

  Future<void> _load({bool more = false}) async {
    if (!_current || _loading) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final page = await ref
          .read(assistantApiProvider(widget.scope))
          .sessions(cursor: more ? _cursor : null);
      if (!_current) return;
      setState(() {
        if (!more) _items.clear();
        for (final item in asList(
          page['items'],
        ).whereType<Map<String, dynamic>>()) {
          final id = asString(item['id']);
          if (id != null) _items[id] = item;
        }
        _cursor = asString(page['next_cursor']);
      });
    } catch (error) {
      if (_current) setState(() => _error = error);
    } finally {
      if (_current) setState(() => _loading = false);
    }
  }

  String _title(I18nState i18n, Map<String, dynamic> item) {
    final title = asString(item['title'])?.trim() ?? '';
    return title.isEmpty ? i18n.t('chat:assistant.link.untitled') : title;
  }

  Future<void> _link(Map<String, dynamic> item) async {
    if (!_current || _sending != null) return;
    final id = asString(item['id'])!;
    final link = asMap(item['link']);
    final i18n = ref.read(i18nProvider);
    setState(() {
      _sending = id;
      _linkError = null;
      _linked = null;
    });
    try {
      await ref
          .read(assistantControllerProvider(widget.scope).notifier)
          .linkExisting(id, asString(link['version'])!);
      if (!_current) return;
      setState(() => _linked = _title(i18n, item));
      await _load();
    } catch (error) {
      if (_current) {
        setState(() => _linkError = error);
        // The conversation changed since it was listed: read it again.
        if (apiErrorOf(error)?.status == 409) await _load();
      }
    } finally {
      if (_current) setState(() => _sending = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final muted = TextStyle(fontSize: FontSizes.sm, color: t.n600);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          i18n.t('chat:assistant.link.description'),
          style: muted.copyWith(height: 1.6),
        ),
        const SizedBox(height: 12),
        if (_linkError ?? _error case final error?)
          Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: Text(
              errorText(i18n, error),
              style: TextStyle(fontSize: FontSizes.sm, color: t.dangerInk),
            ),
          ),
        if (_linked != null)
          Container(
            margin: const EdgeInsets.only(bottom: 10),
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            decoration: BoxDecoration(
              color: t.s100,
              borderRadius: BorderRadius.circular(Radii.md),
            ),
            child: Text(
              i18n.t('chat:assistant.link.success', vars: {'title': _linked!}),
              style: TextStyle(fontSize: FontSizes.sm, color: t.s800),
            ),
          ),
        if (_loading && _items.isEmpty)
          Text(i18n.t('chat:assistant.link.loading'), style: muted),
        for (final item in _items.values) _row(context, i18n, item),
        if (!_loading && _items.isEmpty && _error == null)
          Text(i18n.t('chat:assistant.link.empty'), style: muted),
        if (_cursor != null)
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(
              onPressed: _loading ? null : () => _load(more: true),
              child: Text(
                i18n.t('chat:assistant.link.more'),
                style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
              ),
            ),
          ),
      ],
    );
  }

  Widget _row(BuildContext context, I18nState i18n, Map<String, dynamic> item) {
    final t = context.tokens;
    final link = asMap(item['link']);
    final watched = item['watched'] is bool
        ? item['watched'] as bool
        : link['task_id'] is String && link['archived'] != true;
    final available = link['available'] == true;
    final code = asString(link['reason_code']);
    var reason = i18n.t('chat:assistant.link.unavailable');
    if (code != null) {
      final key = 'chat:assistant.link.reasons.$code';
      final known = i18n.t(key);
      if (known != key) reason = known;
    }
    Widget label(String text) => Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
      decoration: BoxDecoration(
        color: t.n200,
        borderRadius: BorderRadius.circular(4),
      ),
      child: Text(
        text,
        style: TextStyle(fontSize: FontSizes.xs2, color: t.n700),
      ),
    );
    final project = asString(item['project_name']) ?? '';
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(Radii.md),
        border: Border.all(color: t.hair),
      ),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  _title(i18n, item),
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                ),
                const SizedBox(height: 3),
                Wrap(
                  spacing: 6,
                  runSpacing: 4,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    if (project.isNotEmpty)
                      Text(
                        project,
                        style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                      ),
                    if (item['visibility'] == 'workspace')
                      label(i18n.t('chat:assistant.link.workspaceVisible')),
                    if (watched) label(i18n.t('chat:assistant.link.watched')),
                  ],
                ),
                if (!watched && !available)
                  Padding(
                    padding: const EdgeInsets.only(top: 3),
                    child: Text(
                      reason,
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                    ),
                  ),
              ],
            ),
          ),
          // Already followed: nothing to do here; stop following from its
          // task card.
          if (!watched) ...[
            const SizedBox(width: 10),
            OutlinedButton(
              onPressed: _sending != null || !available
                  ? null
                  : () => _link(item),
              style: OutlinedButton.styleFrom(
                side: BorderSide(color: t.hair),
                foregroundColor: t.n800,
                padding: const EdgeInsets.symmetric(
                  horizontal: 12,
                  vertical: 4,
                ),
                minimumSize: const Size(0, 32),
                tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(Radii.full),
                ),
              ),
              child: Text(
                i18n.t(
                  _sending == item['id']
                      ? 'chat:assistant.link.linking'
                      : link['archived'] == true
                      ? 'chat:assistant.link.reopen'
                      : 'chat:assistant.link.action',
                ),
                style: const TextStyle(fontSize: FontSizes.sm),
              ),
            ),
          ],
        ],
      ),
    );
  }
}
