import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/api/api_error.dart';
import '../../shared/api/assistant_profile.dart';
import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/message_part.dart';
import '../../shared/models/session.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/widgets/toast.dart';
import 'api/assistant_api.dart';
import 'state/assistant_controller.dart';
import 'state/stream_store.dart';
import 'utils/compaction_view.dart';
import 'utils/suggestions.dart';
import 'utils/turn_view.dart';
import 'widgets/assistant_intro_entry.dart';
import 'widgets/assistant_notification_target.dart';
import 'widgets/assistant_persona_turn.dart';
import 'widgets/assistant_request_reminder.dart';
import 'widgets/assistant_welcome.dart';
import 'widgets/chat_flow.dart';
import 'widgets/composer/composer.dart';
import 'widgets/composer/resource_slot.dart';
import 'widgets/markdown_view.dart';
import 'widgets/user_bubble.dart';

/// The personal assistant: one long conversation with a secretary (web
/// `AssistantRoute`). What it set in motion shows as cards under its words,
/// tasks live in the top bar's "我的任务" sheet, and what waits on the user
/// is one card at the end of the conversation. It owns no ordinary-chat
/// stream cache or desktop/workbench connection.
class AssistantScreen extends ConsumerStatefulWidget {
  const AssistantScreen({
    super.key,
    required this.scope,
    this.resources,
    this.taskId,
    this.resultId,
    this.intro,
  });
  final AssistantScope scope;
  final ComposerResourceSlot? resources;
  final String? taskId;
  final String? resultId;

  /// `all`: opened from Settings' "重新认识一下" to go through the first
  /// meeting again.
  final String? intro;
  @override
  ConsumerState<AssistantScreen> createState() => _AssistantScreenState();
}

/// Quick prompts under a quiet conversation, for when the last answer
/// offered none: the three things people most often come back for (web
/// `useAssistantQuickPrompts`). A new answer brings them back after one was
/// used.
SuggestionsPart assistantQuickPrompts(I18nState i18n, String? turnKey) =>
    SuggestionsPart(
      id: 'assistant-quick:${turnKey ?? 'start'}',
      items: [
        for (final key in const ['progress', 'waiting', 'remember'])
          NextStepSuggestion(
            label: i18n.t('chat:assistant.quick.$key.label'),
            prompt: i18n.t('chat:assistant.quick.$key.prompt'),
            mode: key == 'remember'
                ? SuggestionMode.draft
                : SuggestionMode.send,
          ),
      ],
    );

class _AssistantScreenState extends ConsumerState<AssistantScreen> {
  final _scroll = ScrollController();
  final _viewport = GlobalKey();
  String? _anchor;

  /// What a welcome card put into the composer.
  ComposerDraft? _draft;

  AssistantController get controller =>
      ref.read(assistantControllerProvider(widget.scope).notifier);
  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  void _fill(String prompt) =>
      setState(() => _draft = ComposerDraft(prompt, (_draft?.nonce ?? 0) + 1));

  /// The first message sent while the first meeting is still open: the
  /// person went straight to work. The meeting steps aside (once they have an
  /// answer, a one-line reminder offers it again).
  void _wentStraightToWork(bool empty) {
    final profile = ref.read(assistantProfileProvider).valueOrNull;
    if (empty && profile != null && introStartsByItself(profile)) {
      ref
          .read(assistantProfileProvider.notifier)
          .recordIntro({'event': 'bypass'})
          .catchError((_) {});
    }
  }

  Future<void> _send(String text, List<String> attachments) async {
    final state = ref.read(assistantControllerProvider(widget.scope));
    _wentStraightToWork(state.messages.isEmpty && !state.hasMore);
    try {
      await controller.send(text, attachments);
    } catch (error) {
      if (mounted) {
        final i18n = ref.read(i18nProvider);
        ref
            .read(toastProvider.notifier)
            .error(
              apiErrorOf(error)?.code == 'ASSISTANT_SEND_UNCERTAIN'
                  ? i18n.t('chat:assistant.sendUncertain')
                  : errorText(i18n, error),
            );
      }
      // The composer keeps what was typed while the send did not land.
      rethrow;
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final state = ref.watch(assistantControllerProvider(widget.scope));
    final i18n = ref.watch(i18nProvider);
    final session = state.snapshot?.session;
    if (session == null) {
      return Center(
        child: state.loading
            ? const CircularProgressIndicator()
            : Padding(
                padding: const EdgeInsets.all(24),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      state.error == null
                          ? i18n.t('common:state.unavailable')
                          : errorText(i18n, state.error!),
                      textAlign: TextAlign.center,
                      style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
                    ),
                    const SizedBox(height: 8),
                    TextButton(
                      onPressed: controller.refresh,
                      child: Text(i18n.t('chat:assistant.reload')),
                    ),
                  ],
                ),
              ),
      );
    }
    // A turn that is only the conversation tidying its own history says
    // nothing, and takes no room.
    final rows = buildChatRows(state.messages)
        .where(
          (row) =>
              row is! AssistantTurnData ||
              row.messages.any((m) => !isCompactionMessage(m)),
        )
        .toList();
    final busy = isBusyStatus(session.status);
    final empty = rows.isEmpty && !busy && !state.hasMore;
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
            child: AssistantPersonaTurn(
              turn: row,
              scope: widget.scope,
              sessionId: session.id,
              streaming: busy && index == rows.length - 1,
              answerWrapper: (id, child) => _VisibleAnswer(
                key: ValueKey(id),
                viewport: _viewport,
                scroll: _scroll,
                onVisible: () => controller.markRead(id),
                child: child,
              ),
            ),
          ),
        },
      if (busy && (rows.isEmpty || rows.last is UserRowData))
        const AssistantPersonaTyping(),
      AssistantRequestReminder(
        key: ValueKey((widget.scope, 'requests')),
        scope: widget.scope,
      ),
    ];
    final lastKey = switch (rows.lastOrNull) {
      UserRowData(:final message) => message.id,
      AssistantTurnData(:final messages) => messages.firstOrNull?.id,
      null => null,
    };
    // "已收到" matters until the reply starts; a pending or uncertain send
    // stays said until it resolves.
    final receipt = state.sending
        ? 'chat:assistant.sendPending'
        : state.sendUncertain
        ? 'chat:assistant.sendUncertain'
        : state.sendAccepted && rows.lastOrNull is UserRowData
        ? 'chat:assistant.sendAccepted'
        : null;
    // Suggestions the last answer offered win over the quick prompts.
    final suggestions = empty
        ? null
        : latestSuggestions(rows, session.status) ??
              (session.status == SessionStatus.idle && rows.isNotEmpty
                  ? assistantQuickPrompts(i18n, lastKey)
                  : null);
    return Column(
      children: [
        if (widget.taskId != null && widget.resultId != null)
          AssistantNotificationTarget(
            key: ValueKey((widget.taskId, widget.resultId)),
            scope: widget.scope,
            taskId: widget.taskId!,
            resultId: widget.resultId!,
          ),
        Expanded(
          child: SizedBox(
            key: _viewport,
            child: empty
                ? AssistantWelcome(onPick: _fill)
                : ChatFlow(
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
        if (receipt != null)
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 2, 20, 0),
            child: Align(
              alignment: Alignment.centerRight,
              child: Semantics(
                liveRegion: true,
                child: Text(
                  i18n.t(receipt),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
              ),
            ),
          ),
        AssistantIntroEntry(
          // Settled after an answer: the person's own words alone are not
          // yet a quiet moment.
          quiet:
              session.status == SessionStatus.idle &&
              rows.lastOrNull is AssistantTurnData &&
              !state.sending,
          onPick: _fill,
          requested: widget.intro,
          onRequestDone: () => GoRouter.maybeOf(context)?.go(Paths.assistant),
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
            suggestions: suggestions,
            draft: _draft,
            onSend: _send,
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
