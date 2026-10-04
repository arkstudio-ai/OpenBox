import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/utils/error_text.dart';
import '../api/assistant_api.dart';
import '../state/assistant_controller.dart';

class AssistantLinkExisting extends ConsumerStatefulWidget {
  const AssistantLinkExisting({super.key, required this.scope});
  final AssistantScope scope;
  @override
  ConsumerState<AssistantLinkExisting> createState() => _LinkState();
}

class _LinkState extends ConsumerState<AssistantLinkExisting> {
  final _items = <String, Map<String, dynamic>>{};
  String? _cursor, _sending, _linked;
  bool _open = false, _loading = false;
  Object? _error;
  bool get _current =>
      mounted &&
      ref.read(authSessionProvider).userId == widget.scope.userId &&
      ref.read(workspaceScopeProvider).currentId == widget.scope.workspaceId;

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

  Future<void> _link(Map<String, dynamic> item) async {
    if (!_current || _sending != null) return;
    final id = asString(item['id'])!;
    final link = asMap(item['link']);
    setState(() {
      _sending = id;
      _error = null;
      _linked = null;
    });
    try {
      await ref
          .read(assistantControllerProvider(widget.scope).notifier)
          .linkExisting(id, asString(link['version'])!);
      if (!_current) return;
      setState(
        () => _linked = asString(item['title'])?.isNotEmpty == true
            ? item['title'] as String
            : id,
      );
      await _load();
    } catch (error) {
      if (_current) setState(() => _error = error);
    } finally {
      if (_current) setState(() => _sending = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    return ExpansionTile(
      title: Text(i18n.t('chat:assistant.link.title')),
      onExpansionChanged: (open) {
        // ExpansionTile can restore its state during build. Apply the change
        // after that frame, with the same actor/workspace check as its reads.
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (!_current || open == _open) return;
          setState(() => _open = open);
          if (open) _load();
        });
      },
      children: [
        if (_open)
          Padding(
            padding: const EdgeInsets.all(12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(i18n.t('chat:assistant.link.description')),
                if (_error != null) Text(errorText(i18n, _error!)),
                if (_linked != null)
                  Text(
                    i18n.t(
                      'chat:assistant.link.success',
                      vars: {'title': _linked!},
                    ),
                  ),
                for (final item in _items.values)
                  Builder(
                    builder: (context) {
                      final link = asMap(item['link']);
                      final linked =
                          link['task_id'] is String && link['archived'] != true;
                      final title = asString(item['title']);
                      return ListTile(
                        contentPadding: EdgeInsets.zero,
                        title: Text(
                          title?.isNotEmpty == true
                              ? title!
                              : item['id'] as String,
                        ),
                        subtitle: Text(
                          [
                            asString(item['project_name']) ?? '',
                            if (link['available'] != true)
                              i18n.t(
                                'chat:assistant.link.reasons.${link['reason_code']}',
                              ),
                          ].join('\n'),
                        ),
                        trailing: TextButton(
                          onPressed:
                              _sending != null ||
                                  linked ||
                                  link['available'] != true
                              ? null
                              : () => _link(item),
                          child: Text(
                            i18n.t(
                              _sending == item['id']
                                  ? 'chat:assistant.link.linking'
                                  : linked
                                  ? 'chat:assistant.link.linked'
                                  : link['archived'] == true
                                  ? 'chat:assistant.link.reopen'
                                  : 'chat:assistant.link.action',
                            ),
                          ),
                        ),
                      );
                    },
                  ),
                if (_loading) const LinearProgressIndicator(),
                if (!_loading && _items.isEmpty)
                  Text(i18n.t('chat:assistant.link.empty')),
                Wrap(
                  children: [
                    TextButton(
                      onPressed: _loading || _sending != null ? null : _load,
                      child: Text(i18n.t('chat:assistant.reload')),
                    ),
                    if (_cursor != null)
                      TextButton(
                        onPressed: _loading ? null : () => _load(more: true),
                        child: Text(i18n.t('chat:assistant.moreTasks')),
                      ),
                  ],
                ),
              ],
            ),
          ),
      ],
    );
  }
}
