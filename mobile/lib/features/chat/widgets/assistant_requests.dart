import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/events/app_lifecycle.dart';
import '../../../shared/events/bus.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/interaction.dart';
import '../../../shared/models/json.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/ws/ws_client.dart';
import '../api/assistant_api.dart';
import '../api/assistant_reply.dart';
import '../state/question_draft.dart';
import 'cards/permission_card.dart';
import 'cards/question_dock.dart';

/// The same versioned cards as the original execution page, grouped with the
/// task/project returned by the authorized assistant request projection.
class AssistantRequests extends ConsumerStatefulWidget {
  const AssistantRequests({super.key, required this.scope, required this.kind});
  final AssistantScope scope;
  final String kind;
  @override
  ConsumerState<AssistantRequests> createState() => _AssistantRequestsState();
}

class _AssistantRequestsState extends ConsumerState<AssistantRequests> {
  List<Map<String, dynamic>> _items = [], _receipts = [];
  String? _cursor;
  int _pages = 1;
  bool _loading = false, _failed = false;
  Timer? _timer;
  StreamSubscription<WsEvent>? _ws;
  StreamSubscription<AppEvent>? _bus;
  bool get _current =>
      mounted && ref.read(assistantScopeProvider) == widget.scope;
  @override
  void initState() {
    super.initState();
    Future.microtask(_load);
    _timer = Timer.periodic(const Duration(seconds: 5), (_) {
      if (ref.read(appVisibleProvider)) unawaited(_load());
    });
    _ws = ref.read(wsClientProvider).events.listen((event) {
      if (event.type == '__connected' ||
          event.type.startsWith('assistant.') ||
          event.type.startsWith('question.') ||
          event.type.startsWith('permission.')) {
        if (ref.read(appVisibleProvider)) unawaited(_load());
      }
    });
    _bus = ref.read(appEventBusProvider).stream.listen((event) {
      if (event.type == 'question.resolved' ||
          event.type == 'assistant.request.changed') {
        unawaited(_load());
      }
    });
    ref.listenManual(appVisibleProvider, (previous, visible) {
      if (visible && previous == false) unawaited(_load());
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    unawaited(_ws?.cancel());
    unawaited(_bus?.cancel());
    super.dispose();
  }

  Future<void> _load({bool more = false}) async {
    if (!_current || _loading) return;
    setState(() => _loading = true);
    try {
      final count = _pages + (more ? 1 : 0);
      final items = <Map<String, dynamic>>[];
      final receipts = <Map<String, dynamic>>[];
      String? cursor;
      for (var index = 0; index < count; index++) {
        final page = await ref
            .read(assistantApiProvider(widget.scope))
            .requests(widget.kind, cursor: cursor);
        if (!_current) return;
        items.addAll(asList(page['items']).whereType<Map<String, dynamic>>());
        if (index == 0) {
          receipts.addAll(
            asList(page['receipts']).whereType<Map<String, dynamic>>(),
          );
        }
        cursor = asString(page['next_cursor']);
        if (cursor == null) break;
      }
      if (widget.kind == 'question') {
        for (final item in items) {
          ref
              .read(questionDraftProvider.notifier)
              .hydrate(QuestionRequest.fromJson(item));
        }
      }
      setState(() {
        _items = items;
        _receipts = receipts;
        _cursor = cursor;
        _pages = count;
        _failed = false;
      });
    } catch (_) {
      if (mounted) {
        setState(() {
          _items = [];
          _receipts = [];
          _failed = true;
        });
      }
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (ref.watch(assistantScopeProvider) != widget.scope) {
      return const SizedBox.shrink();
    }
    final i18n = ref.watch(i18nProvider);
    if (_failed) {
      return TextButton(
        onPressed: _load,
        child: Text(i18n.t('chat:assistant.reload')),
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final item in _items)
          Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 14),
                child: Row(
                  children: [
                    Expanded(
                      child: Text(
                        '${item['task_title'] ?? ''} · ${item['project_name'] ?? ''}',
                      ),
                    ),
                    TextButton(
                      onPressed: () => context.push(
                        Paths.chat(item['session_id'] as String),
                      ),
                      child: Text(i18n.t('chat:assistant.requests.openTask')),
                    ),
                  ],
                ),
              ),
              if (widget.kind == 'permission')
                PermissionCard(
                  key: ValueKey(
                    '${item['id']}:${asMap(item['assistant'])['request_revision']}',
                  ),
                  request: PermissionRequest.fromJson(item),
                )
              else
                QuestionDock(
                  key: ValueKey(
                    '${item['id']}:${asMap(item['assistant'])['request_revision']}',
                  ),
                  request: QuestionRequest.fromJson(item),
                ),
            ],
          ),
        if (_cursor != null)
          TextButton(
            onPressed: _loading ? null : () => _load(more: true),
            child: Text(
              i18n.t(
                widget.kind == 'permission'
                    ? 'chat:assistant.requests.morePermissions'
                    : 'chat:assistant.requests.more',
              ),
            ),
          ),
        if (_receipts.isNotEmpty)
          ExpansionTile(
            title: Text(i18n.t('chat:assistant.requests.receipts')),
            children: [
              for (final receipt in _receipts)
                ListTile(
                  title: Text(i18n.t(replyStateKey(receipt['state']))),
                  subtitle: Text(asString(receipt['command_id']) ?? ''),
                  trailing: receipt['session_id'] is String
                      ? TextButton(
                          onPressed: () => context.push(
                            Paths.chat(receipt['session_id'] as String),
                          ),
                          child: Text(
                            i18n.t('chat:assistant.requests.openTask'),
                          ),
                        )
                      : null,
                ),
            ],
          ),
      ],
    );
  }
}
