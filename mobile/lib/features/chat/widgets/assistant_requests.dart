import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/events/app_lifecycle.dart';
import '../../../shared/events/bus.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/interaction.dart';
import '../../../shared/models/json.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/ws/ws_client.dart';
import '../api/assistant_api.dart';
import '../state/question_draft.dart';
import 'assistant_request_review.dart';
import 'cards/permission_card.dart';
import 'cards/question_dock.dart';

const _kinds = ['question', 'permission'];

/// One kind of pending request, as last read: the pages loaded so far and
/// the first page's reply receipts.
class _Requests {
  List<Map<String, dynamic>> items = [], receipts = [];
  String? cursor;
  int pages = 1;
  bool loading = false;
  Object? error;
}

/// What waits on the user across the conversations the assistant follows,
/// at the end of the assistant conversation like a secretary's "these need
/// you" (web `AssistantRequests`): the original question and approval cards,
/// plus questions waiting in the user's other conversations. Only a reply
/// that failed gets a line; nothing renders when nothing waits.
class AssistantRequests extends ConsumerStatefulWidget {
  const AssistantRequests({super.key, required this.scope, this.beforeOpen});
  final AssistantScope scope;
  final VoidCallback? beforeOpen;
  @override
  ConsumerState<AssistantRequests> createState() => _AssistantRequestsState();
}

class _AssistantRequestsState extends ConsumerState<AssistantRequests> {
  final _requests = {for (final kind in _kinds) kind: _Requests()};
  List<Map<String, dynamic>> _waiting = [];
  bool _waitingLoading = false;
  int _ticks = 0;
  Timer? _timer;
  StreamSubscription<WsEvent>? _ws;
  StreamSubscription<AppEvent>? _bus;
  bool get _current =>
      mounted && ref.read(assistantScopeProvider) == widget.scope;

  @override
  void initState() {
    super.initState();
    Future.microtask(_loadAll);
    _timer = Timer.periodic(const Duration(seconds: 5), (_) {
      if (!ref.read(appVisibleProvider)) return;
      for (final kind in _kinds) {
        unawaited(_load(kind));
      }
      // Other conversations' questions are a slower, secondary read.
      if (++_ticks % 6 == 0) unawaited(_loadWaiting());
    });
    _ws = ref.read(wsClientProvider).events.listen((event) {
      if (event.type == '__connected' ||
          event.type.startsWith('assistant.') ||
          event.type.startsWith('question.') ||
          event.type.startsWith('permission.')) {
        if (!ref.read(appVisibleProvider)) return;
        for (final kind in _kinds) {
          unawaited(_load(kind));
        }
        if (!event.type.startsWith('permission.')) unawaited(_loadWaiting());
      }
    });
    _bus = ref.read(appEventBusProvider).stream.listen((event) {
      if (event.type == 'question.resolved' ||
          event.type == 'assistant.request.changed') {
        unawaited(_loadAll());
      }
    });
    ref.listenManual(appVisibleProvider, (previous, visible) {
      if (visible && previous == false) unawaited(_loadAll());
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    unawaited(_ws?.cancel());
    unawaited(_bus?.cancel());
    super.dispose();
  }

  Future<void> _loadAll() async {
    await Future.wait([for (final kind in _kinds) _load(kind), _loadWaiting()]);
  }

  Future<void> _load(String kind, {bool more = false}) async {
    final state = _requests[kind]!;
    if (!_current || state.loading) return;
    setState(() => state.loading = true);
    try {
      final count = state.pages + (more ? 1 : 0);
      final items = <Map<String, dynamic>>[];
      final receipts = <Map<String, dynamic>>[];
      String? cursor;
      for (var index = 0; index < count; index++) {
        final page = await ref
            .read(assistantApiProvider(widget.scope))
            .requests(kind, cursor: cursor);
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
      if (kind == 'question') {
        for (final item in items) {
          ref
              .read(questionDraftProvider.notifier)
              .hydrate(QuestionRequest.fromJson(item));
        }
      }
      setState(() {
        state
          ..items = items
          ..receipts = receipts
          ..cursor = cursor
          ..pages = count
          ..error = null;
      });
    } catch (error) {
      // A failed authoritative read removes stale cards rather than leaving
      // a request that may already be gone answerable.
      if (mounted) {
        setState(() {
          state
            ..items = []
            ..receipts = []
            ..error = error;
        });
      }
    } finally {
      if (mounted) setState(() => state.loading = false);
    }
  }

  Future<void> _loadWaiting() async {
    if (!_current || _waitingLoading) return;
    _waitingLoading = true;
    try {
      final page = await ref
          .read(assistantApiProvider(widget.scope))
          .waitingQuestions();
      if (!_current) return;
      setState(
        () => _waiting = asList(
          page['items'],
        ).whereType<Map<String, dynamic>>().toList(),
      );
    } catch (_) {
      // Secondary: the conversations themselves still hold their questions.
    } finally {
      _waitingLoading = false;
    }
  }

  void _openConversation(String sessionId) {
    final router = GoRouter.of(context);
    widget.beforeOpen?.call();
    router.push(Paths.chat(sessionId));
  }

  @override
  Widget build(BuildContext context) {
    if (ref.watch(assistantScopeProvider) != widget.scope) {
      return const SizedBox.shrink();
    }
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final questions = _requests['question']!;
    final permissions = _requests['permission']!;
    final error = questions.error ?? permissions.error;
    final failed = [
      ...questions.receipts,
      ...permissions.receipts,
    ].where((receipt) => receipt['state'] == 'failed').toList();
    final count =
        questions.items.length + permissions.items.length + _waiting.length;
    if (error == null &&
        count == 0 &&
        failed.isEmpty &&
        questions.cursor == null &&
        permissions.cursor == null) {
      return const SizedBox.shrink();
    }
    final muted = TextStyle(fontSize: FontSizes.sm, color: t.n700);
    return Semantics(
      container: true,
      label: i18n.t('chat:assistant.requests.label'),
      child: Container(
        padding: const EdgeInsets.fromLTRB(14, 14, 14, 10),
        decoration: BoxDecoration(
          color: t.a100,
          borderRadius: BorderRadius.circular(Radii.xl),
          border: Border.all(color: t.hair),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Icon(
                  Icons.notifications_active_outlined,
                  size: 17,
                  color: t.dangerInk,
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    // Only a failed reply or an unread page left: no "0 件事".
                    count == 0
                        ? i18n.t('chat:assistant.requests.label')
                        : i18n.t(
                            'chat:assistant.requests.title',
                            vars: {'count': count},
                          ),
                    style: TextStyle(
                      fontSize: FontSizes.base,
                      fontWeight: FontWeight.w500,
                      color: t.dangerInk,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 8),
            if (error != null) ...[
              Text(
                errorText(i18n, error),
                style: TextStyle(fontSize: FontSizes.sm, color: t.dangerInk),
              ),
              _TextLink(
                label: i18n.t('chat:assistant.reload'),
                onTap: () => unawaited(_loadAll()),
              ),
            ] else ...[
              for (final item in questions.items)
                _Request(
                  key: ValueKey(
                    'question:${item['id']}:${asMap(item['assistant'])['request_revision']}',
                  ),
                  scope: widget.scope,
                  kind: 'question',
                  item: item,
                  onOpen: _openConversation,
                ),
              if (questions.cursor != null)
                _TextLink(
                  label: i18n.t('chat:assistant.requests.more'),
                  onTap: questions.loading
                      ? null
                      : () => _load('question', more: true),
                ),
              for (final item in permissions.items)
                _Request(
                  key: ValueKey(
                    'permission:${item['id']}:${asMap(item['assistant'])['request_revision']}',
                  ),
                  scope: widget.scope,
                  kind: 'permission',
                  item: item,
                  onOpen: _openConversation,
                ),
              if (permissions.cursor != null)
                _TextLink(
                  label: i18n.t('chat:assistant.requests.morePermissions'),
                  onTap: permissions.loading
                      ? null
                      : () => _load('permission', more: true),
                ),
              if (_waiting.isNotEmpty) ...[
                Padding(
                  padding: const EdgeInsets.only(top: 6, bottom: 6),
                  child: Text(
                    i18n.t('chat:assistant.requests.otherConversations'),
                    style: muted,
                  ),
                ),
                for (final item in _waiting)
                  _Waiting(
                    key: ValueKey('waiting:${item['id']}'),
                    item: item,
                    onOpen: _openConversation,
                  ),
                Padding(
                  padding: const EdgeInsets.only(top: 2, bottom: 4),
                  child: Text(
                    i18n.t('chat:assistant.requests.askMe'),
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                  ),
                ),
              ],
              for (final receipt in failed)
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Wrap(
                    crossAxisAlignment: WrapCrossAlignment.center,
                    spacing: 8,
                    children: [
                      Text(
                        i18n.t('chat:assistant.requests.failed'),
                        style: muted,
                      ),
                      if (receipt['session_id'] case final String session)
                        _TextLink(
                          label: i18n.t('chat:assistant.requests.openTask'),
                          onTap: () => _openConversation(session),
                        ),
                    ],
                  ),
                ),
            ],
          ],
        ),
      ),
    );
  }
}

/// One original question or approval card, with whose it is and where it
/// runs.
class _Request extends ConsumerWidget {
  const _Request({
    super.key,
    required this.scope,
    required this.kind,
    required this.item,
    required this.onOpen,
  });
  final AssistantScope scope;
  final String kind;
  final Map<String, dynamic> item;
  final ValueChanged<String> onOpen;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final rawTitle = asString(item['task_title'])?.trim() ?? '';
    final title = rawTitle.isEmpty
        ? i18n.t('chat:assistant.requests.untitled')
        : rawTitle;
    final project = asString(item['project_name'])?.trim() ?? '';
    final session = asString(item['session_id']);
    // The question dock carries its own side margin; line up with it.
    final inset = EdgeInsets.symmetric(horizontal: kind == 'question' ? 12 : 0);
    final revision = asMap(item['assistant'])['request_revision'];
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: inset.add(const EdgeInsets.only(bottom: 4)),
            child: Row(
              children: [
                Expanded(
                  child: Text.rich(
                    TextSpan(
                      children: [
                        TextSpan(
                          text: i18n.t(
                            kind == 'question'
                                ? 'chat:assistant.requests.asks'
                                : 'chat:assistant.requests.needsApproval',
                            vars: {'title': title},
                          ),
                        ),
                        if (project.isNotEmpty)
                          TextSpan(
                            text: ' · $project',
                            style: TextStyle(color: t.n600),
                          ),
                      ],
                    ),
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
                  ),
                ),
                if (session != null)
                  _TextLink(
                    label: i18n.t('chat:assistant.requests.openTask'),
                    onTap: () => onOpen(session),
                  ),
              ],
            ),
          ),
          if (kind == 'permission')
            PermissionCard(
              key: ValueKey('${item['id']}:$revision'),
              request: PermissionRequest.fromJson(item),
            )
          else
            QuestionDock(
              key: ValueKey('${item['id']}:$revision'),
              request: QuestionRequest.fromJson(item),
            ),
          if (item['assistant'] is Map<String, dynamic>)
            Padding(
              padding: inset,
              child: Align(
                alignment: Alignment.centerLeft,
                child: AssistantRequestReviewButton(
                  scope: scope,
                  kind: kind,
                  requestId: item['id'] as String,
                  binding: asMap(item['assistant']),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

/// A question waiting in another conversation: answered there, or by the
/// assistant when asked.
class _Waiting extends ConsumerWidget {
  const _Waiting({super.key, required this.item, required this.onOpen});
  final Map<String, dynamic> item;
  final ValueChanged<String> onOpen;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final rawTitle = asString(item['session_title'])?.trim() ?? '';
    final project = asString(item['project_name'])?.trim() ?? '';
    final source = [
      rawTitle.isEmpty ? i18n.t('chat:assistant.requests.untitled') : rawTitle,
      if (project.isNotEmpty) project,
    ].join(' · ');
    final questions = asList(item['questions']);
    final question = questions.isEmpty
        ? ''
        : asString(asMap(questions.first)['question']) ?? '';
    final session = asString(item['session_id']);
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.fromLTRB(12, 10, 8, 10),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.md),
        border: Border.all(color: t.hair),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  source,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
                if (question.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      question,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
                    ),
                  ),
              ],
            ),
          ),
          if (session != null) ...[
            const SizedBox(width: 8),
            OutlinedButton(
              onPressed: () => onOpen(session),
              style: OutlinedButton.styleFrom(
                side: BorderSide(color: t.hair),
                foregroundColor: t.n800,
                padding: const EdgeInsets.symmetric(horizontal: 12),
                minimumSize: const Size(0, 30),
                tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(Radii.full),
                ),
              ),
              child: Text(
                i18n.t('chat:assistant.requests.answerThere'),
                style: const TextStyle(fontSize: FontSizes.xs),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _TextLink extends StatelessWidget {
  const _TextLink({required this.label, required this.onTap});
  final String label;
  final VoidCallback? onTap;
  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return TextButton(
      onPressed: onTap,
      style: TextButton.styleFrom(
        foregroundColor: t.n700,
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
        minimumSize: Size.zero,
        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
      ),
      child: Text(
        label,
        style: const TextStyle(
          fontSize: FontSizes.xs,
          decoration: TextDecoration.underline,
        ),
      ),
    );
  }
}
