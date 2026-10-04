import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/api/api_error.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/widgets/toast.dart';
import 'api/assistant_api.dart';
import 'state/assistant_controller.dart';
import 'state/stream_store.dart';
import 'utils/turn_view.dart';
import 'widgets/assistant_requests.dart';
import 'widgets/assistant_task_card.dart';
import 'widgets/assistant_task_receipts.dart';
import 'widgets/assistant_turn.dart';
import 'widgets/chat_flow.dart';
import 'widgets/composer/composer.dart';
import 'widgets/composer/resource_slot.dart';
import 'widgets/markdown_view.dart';
import 'widgets/typing_row.dart';
import 'widgets/user_bubble.dart';

/// Fixed private entry, using the existing transcript and composer components.
/// It owns no ordinary-chat stream cache or desktop/workbench connection.
class AssistantScreen extends ConsumerStatefulWidget {
  const AssistantScreen({super.key, required this.scope, this.resources});
  final AssistantScope scope;
  final ComposerResourceSlot? resources;
  @override
  ConsumerState<AssistantScreen> createState() => _AssistantScreenState();
}

class _AssistantScreenState extends ConsumerState<AssistantScreen> {
  final _scroll = ScrollController();
  final _viewport = GlobalKey();
  String? _anchor;
  AssistantController get controller =>
      ref.read(assistantControllerProvider(widget.scope).notifier);
  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _act(Future<void> Function() action) async {
    try {
      await action();
    } catch (error) {
      if (mounted) {
        final i18n = ref.read(i18nProvider);
        final code = apiErrorOf(error)?.code;
        ref
            .read(toastProvider.notifier)
            .error(
              code == 'ASSISTANT_SEND_UNCERTAIN'
                  ? i18n.t('chat:assistant.sendUncertain')
                  : apiErrorOf(error)?.status == 409
                  ? i18n.t('chat:assistant.task.changed')
                  : errorText(i18n, error),
            );
      }
      rethrow;
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(assistantControllerProvider(widget.scope));
    final i18n = ref.watch(i18nProvider);
    final session = state.snapshot?.session;
    if (session == null) {
      return Center(
        child: state.loading
            ? const CircularProgressIndicator()
            : Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(i18n.t('chat:assistant.sourceUnavailable')),
                  TextButton(
                    onPressed: controller.refresh,
                    child: Text(i18n.t('chat:assistant.reload')),
                  ),
                ],
              ),
      );
    }
    final rows = buildChatRows(state.messages);
    final busy = isBusyStatus(session.status);
    _anchor ??= rows.whereType<UserRowData>().firstOrNull?.message.id;
    final anchor = rows.indexWhere(
      (r) => r is UserRowData && r.message.id == _anchor,
    );
    final widgets = <Widget>[
      for (final (index, row) in rows.indexed)
        switch (row) {
          UserRowData(:final message) => UserBubble(message: message),
          AssistantTurnData() => CopyAuthority(
            check: (text) => controller.canCopy(
              row.messages.map((m) => m.id).toList(),
              text,
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                if (row.messages.any((m) => m.sourceStatus == 'unavailable'))
                  Text(i18n.t('chat:assistant.sourceUnavailable')),
                if (row.messages.any((m) => m.sourceStatus == 'pending'))
                  Text(i18n.t('chat:assistant.sourcePending')),
                AssistantTurn(
                  turn: row,
                  sessionId: session.id,
                  streaming: busy && index == rows.length - 1,
                  immutableHistory: true,
                  taskReceipts: AssistantTaskReceipts(
                    scope: widget.scope,
                    parts: [for (final m in row.messages) ...m.parts],
                    onAction: _act,
                  ),
                  onReview: () {},
                  onRegenerate: (_) {},
                  onDismiss: (_) {},
                  answerWrapper: (id, child) => _VisibleAnswer(
                    key: ValueKey(id),
                    viewport: _viewport,
                    scroll: _scroll,
                    onVisible: () => controller.markRead(id),
                    child: child,
                  ),
                ),
              ],
            ),
          ),
        },
      if (busy && (rows.isEmpty || rows.last is UserRowData)) const TypingRow(),
      AssistantRequests(
        key: ValueKey((widget.scope, 'question')),
        scope: widget.scope,
        kind: 'question',
      ),
      AssistantRequests(
        key: ValueKey((widget.scope, 'permission')),
        scope: widget.scope,
        kind: 'permission',
      ),
      if (state.tasks.isNotEmpty)
        ExpansionTile(
          key: const PageStorageKey('assistant-tasks'),
          expandedCrossAxisAlignment: CrossAxisAlignment.stretch,
          title: Text(i18n.t('chat:assistant.tasks')),
          children: [
            for (final task in state.tasks)
              AssistantTaskCard(
                key: ValueKey(task.id),
                task: task,
                scope: widget.scope,
                lastSeen: state.snapshot!.lastSeen,
                pending: state.actionPending[task.id],
                onControl: (action) =>
                    _act(() => controller.control(task, action)),
                onRetry: () => _act(() => controller.retryReport(task)),
              ),
            if (state.taskCursor != null)
              TextButton(
                onPressed: () => _act(controller.moreTasks).ignore(),
                child: Text(i18n.t('chat:assistant.moreTasks')),
              ),
          ],
        ),
    ];
    return Column(
      children: [
        Expanded(
          child: SizedBox(
            key: _viewport,
            child: ChatFlow(
              rows: widgets,
              controller: _scroll,
              olderCount: anchor < 0 ? 0 : anchor,
              topKey: state.messages.firstOrNull?.id,
              forceScrollToken: rows
                  .whereType<UserRowData>()
                  .lastOrNull
                  ?.message
                  .id,
              onNearTop: state.hasMore ? controller.loadOlder : null,
              loadingOlder: state.loadingOlder,
            ),
          ),
        ),
        if (state.sending || state.sendUncertain || state.sendAccepted)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
            child: Text(
              i18n.t(
                state.sending
                    ? 'chat:assistant.sendPending'
                    : state.sendUncertain
                    ? 'chat:assistant.sendUncertain'
                    : 'chat:assistant.sendAccepted',
              ),
            ),
          ),
        SafeArea(
          top: false,
          child: Composer(
            key: ValueKey(session.id),
            sessionKey: session.id,
            session: session,
            busy: busy,
            assistant: true,
            historyController: _scroll,
            resources: widget.resources,
            onSend: (text, attachments) =>
                _act(() => controller.send(text, attachments)),
          ),
        ),
      ],
    );
  }
}

/// Only the final answer's actual viewport intersection can submit a signed
/// display token. Fetching, laying out offscreen, modal overlays and background
/// lifecycle transitions do not mark anything read.
class _VisibleAnswer extends StatefulWidget {
  const _VisibleAnswer({
    super.key,
    required this.viewport,
    required this.scroll,
    required this.onVisible,
    required this.child,
  });
  final GlobalKey viewport;
  final ScrollController scroll;
  final Future<void> Function() onVisible;
  final Widget child;
  @override
  State<_VisibleAnswer> createState() => _VisibleAnswerState();
}

class _VisibleAnswerState extends State<_VisibleAnswer>
    with WidgetsBindingObserver {
  @override
  void initState() {
    super.initState();
    widget.scroll.addListener(_check);
    WidgetsBinding.instance.addObserver(this);
    _schedule();
  }

  @override
  void didUpdateWidget(_VisibleAnswer oldWidget) {
    super.didUpdateWidget(oldWidget);
    _schedule();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _schedule();
  }

  void _schedule() =>
      WidgetsBinding.instance.addPostFrameCallback((_) => _check());
  void _check() {
    if (!mounted ||
        WidgetsBinding.instance.lifecycleState != AppLifecycleState.resumed ||
        ModalRoute.of(context)?.isCurrent != true) {
      return;
    }
    final box = context.findRenderObject();
    final viewport = widget.viewport.currentContext?.findRenderObject();
    if (box is! RenderBox ||
        viewport is! RenderBox ||
        !box.hasSize ||
        !viewport.hasSize) {
      return;
    }
    final bounds = box.localToGlobal(Offset.zero) & box.size;
    final visible = viewport.localToGlobal(Offset.zero) & viewport.size;
    if (bounds.overlaps(visible) && bounds.intersect(visible).height >= 16) {
      unawaited(widget.onVisible());
    }
  }

  @override
  void dispose() {
    widget.scroll.removeListener(_check);
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.child;
}
