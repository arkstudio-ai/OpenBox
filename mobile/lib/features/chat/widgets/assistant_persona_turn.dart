import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/assistant_profile.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';
import '../../../shared/widgets/toast.dart';
import '../api/assistant_api.dart';
import '../utils/assistant_activity.dart';
import '../utils/assistant_text.dart';
import '../utils/compaction_view.dart';
import '../utils/content_view.dart';
import '../utils/task_status.dart';
import '../utils/turn_view.dart';
import 'ai_disclosure.dart';
import 'assistant_avatar.dart';
import 'assistant_memory_receipts.dart';
import 'assistant_task_receipts.dart';
import 'markdown_view.dart';
import 'recalled_memories.dart';
import 'result_artifacts.dart';
import 'traces/work_log_trace.dart';
import 'typing_row.dart';

/// Indent of everything the assistant says, under its name.
const _bodyInset = 32.0;

/// The personal assistant speaks like a person (web `PersonaTurn`): its name
/// and face, its words, and cards for what it set in motion. How it got
/// there — tool calls, context size, model, tokens — stays off the page.
class AssistantPersonaTurn extends ConsumerWidget {
  const AssistantPersonaTurn({
    super.key,
    required this.turn,
    required this.scope,
    required this.sessionId,
    required this.streaming,
    this.answerWrapper,
  });

  final AssistantTurnData turn;
  final AssistantScope scope;
  final String sessionId;

  /// This is the live turn and the conversation is busy.
  final bool streaming;

  /// Wraps the final answer, e.g. to learn when it was actually seen.
  final Widget Function(String messageId, Widget child)? answerWrapper;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final replies = turn.messages
        .where((message) => !isCompactionMessage(message))
        .toList();
    // A turn that is only the conversation tidying its own history has
    // nothing to say.
    if (replies.isEmpty) return const SizedBox.shrink();
    final content = buildAssistantContentView(turn.messages, streaming);
    final parts = [for (final message in replies) ...message.parts];
    final calls = [
      for (final part in parts)
        if (part is ToolPart || part is SubtaskPart) part,
    ];
    final preAnswer = streaming && !content.hasFinal;
    // A report and later coordination can share one visible turn; the
    // answer's actions belong to the message that holds it.
    final answer =
        replies.where((m) => m.id == content.finalMessageId).firstOrNull ??
        replies.last;
    final answerStreaming =
        streaming && answer.id == turn.lastMessageId && answer.finish == null;
    // Older answers may still name internal ids; the screen never shows them.
    final finalText = hideInternalIds(content.finalText);
    final markdown = MarkdownView(
      finalText,
      key: ValueKey(content.finalMessageId),
      streaming: answerStreaming,
    );
    return Column(
      key: const ValueKey('assistant-persona-turn'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        AssistantPersonaHeader(
          origin: turn.origin,
          // Labelled as AI-generated like any assistant answer (web
          // `showAiLabel`).
          aiLabel:
              streaming ||
              content.hasFinal ||
              content.workEvents.isNotEmpty ||
              content.resultGroups.isNotEmpty,
        ),
        Padding(
          padding: const EdgeInsets.only(left: _bodyInset),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              WorkLogTrace(events: content.workEvents, active: preAnswer),
              if (preAnswer)
                Align(
                  alignment: Alignment.centerLeft,
                  child: ThinkingRow(
                    label: i18n.t(
                      'chat:assistant.activity.${assistantActivity(calls)}',
                    ),
                  ),
                )
              else if (content.hasFinal)
                answerWrapper != null && content.finalMessageId != null
                    ? answerWrapper!(content.finalMessageId!, markdown)
                    : markdown,
              AssistantTaskReceipts(scope: scope, parts: parts),
              AssistantMemoryReceipts(scope: scope, parts: parts),
              RecalledMemories(
                scope: scope,
                sessionId: sessionId,
                messageId: replies.first.parentId,
                streaming: answerStreaming,
              ),
              if (content.incomplete && turn.error == null)
                const _IncompleteNotice(),
              if (turn.error != null && !streaming)
                AssistantErrorNotice(error: turn.error!),
              ResultArtifacts(
                groups: content.resultGroups,
                verification: content.verification,
              ),
              if (content.hasFinal && !preAnswer)
                AssistantAnswerMeta(
                  key: ValueKey('meta-${answer.id}'),
                  scope: scope,
                  sessionId: sessionId,
                  message: answer,
                  text: finalText,
                  streaming: answerStreaming,
                ),
            ],
          ),
        ),
      ],
    );
  }
}

/// The assistant's face and name, with what started an answer nobody asked
/// for: a task's progress or the daily briefing.
class AssistantPersonaHeader extends ConsumerWidget {
  const AssistantPersonaHeader({super.key, this.origin, this.aiLabel = false});
  final String? origin;

  /// Show the AI-generated label beside the name.
  final bool aiLabel;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Row(
        children: [
          const AssistantAvatar(),
          const SizedBox(width: 8),
          // Name, origin and the AI label wrap onto a second line on a narrow
          // screen rather than overflow.
          Flexible(
            child: Wrap(
              spacing: 8,
              runSpacing: 4,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                Text(
                  // The name the person gave it, at once wherever it was set.
                  assistantLabel(ref),
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w500,
                    color: t.ink,
                  ),
                ),
                if (origin != null)
                  Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 8,
                      vertical: 2,
                    ),
                    decoration: BoxDecoration(
                      color: t.hairSoft,
                      borderRadius: BorderRadius.circular(Radii.full),
                    ),
                    child: Text(
                      i18n.t('chat:assistant.origin.$origin'),
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
                    ),
                  ),
                if (aiLabel) const AiGeneratedLabel(),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// Before the assistant's first words of a reply (web `TypingRow`, persona
/// variant).
class AssistantPersonaTyping extends ConsumerWidget {
  const AssistantPersonaTyping({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const AssistantPersonaHeader(),
        Padding(
          padding: const EdgeInsets.only(left: _bodyInset),
          child: Align(
            alignment: Alignment.centerLeft,
            child: ThinkingRow(
              label: i18n.t('chat:assistant.activity.thinking'),
            ),
          ),
        ),
      ],
    );
  }
}

/// A failed turn, said plainly (web `InlineErrorCard` for the assistant's
/// conversations): the raw error stays one tap away.
class AssistantErrorNotice extends ConsumerStatefulWidget {
  const AssistantErrorNotice({super.key, required this.error});
  final Map<String, dynamic> error;
  @override
  ConsumerState<AssistantErrorNotice> createState() =>
      _AssistantErrorNoticeState();
}

class _AssistantErrorNoticeState extends ConsumerState<AssistantErrorNotice> {
  bool _details = false;

  String get _message {
    final message = widget.error['message'];
    if (message is String && message.trim().isNotEmpty) return message;
    final nested = widget.error['error'];
    if (nested is String && nested.trim().isNotEmpty) return nested;
    try {
      return jsonEncode(widget.error);
    } on Object {
      return '';
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final message = _message;
    return Container(
      margin: const EdgeInsets.only(top: 8),
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 10),
      decoration: BoxDecoration(
        color: t.dangerSoft,
        borderRadius: BorderRadius.circular(Radii.md),
        border: Border.all(color: t.hair),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: 1, right: 10),
            child: Icon(Icons.error_outline, size: 16, color: t.danger),
          ),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  i18n.t('chat:meta.errorTitle'),
                  style: TextStyle(
                    fontSize: FontSizes.md,
                    fontWeight: FontWeight.w500,
                    color: t.dangerInk,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  i18n.t('chat:assistant.continueAfterError'),
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    height: 1.5,
                    color: t.n700,
                  ),
                ),
                if (message.isNotEmpty) ...[
                  GestureDetector(
                    onTap: () => setState(() => _details = !_details),
                    child: Padding(
                      padding: const EdgeInsets.only(top: 6),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Text(
                            i18n.t('chat:meta.errorDetails'),
                            style: TextStyle(
                              fontSize: FontSizes.xs,
                              color: t.n600,
                            ),
                          ),
                          Icon(
                            _details ? Icons.expand_less : Icons.expand_more,
                            size: 14,
                            color: t.n600,
                          ),
                        ],
                      ),
                    ),
                  ),
                  if (_details)
                    Padding(
                      padding: const EdgeInsets.only(top: 4),
                      child: SelectableText(
                        message,
                        style: TextStyle(
                          fontSize: FontSizes.xs,
                          height: 1.5,
                          color: t.n600,
                        ),
                      ),
                    ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// "今天 14:05", else the date and time (web `MessageTimestamp`).
String messageTime(I18nState i18n, DateTime? createdAt, {DateTime? now}) {
  if (createdAt == null) return '';
  final at = createdAt.toLocal();
  final current = now ?? DateTime.now();
  final time =
      '${at.hour.toString().padLeft(2, '0')}:${at.minute.toString().padLeft(2, '0')}';
  if (at.year == current.year &&
      at.month == current.month &&
      at.day == current.day) {
    return i18n.t('common:time.todayTime', vars: {'time': time});
  }
  return i18n.t(
    'common:time.fullDateTime',
    vars: {'date': shortDate(at, i18n.language, year: true), 'time': time},
  );
}

/// Under an answer: copy, like, dislike and the time (web `AssistantMeta`,
/// minimal). No model, token or latency badges.
class AssistantAnswerMeta extends ConsumerStatefulWidget {
  const AssistantAnswerMeta({
    super.key,
    required this.scope,
    required this.sessionId,
    required this.message,
    required this.text,
    required this.streaming,
  });
  final AssistantScope scope;
  final String sessionId;
  final ChatMessage message;
  final String text;
  final bool streaming;
  @override
  ConsumerState<AssistantAnswerMeta> createState() =>
      _AssistantAnswerMetaState();
}

class _AssistantAnswerMetaState extends ConsumerState<AssistantAnswerMeta> {
  late String? _reaction = widget.message.reaction;

  /// Asked right after a thumbs-down; answering is optional.
  bool _asking = false;
  String? _reason;

  @override
  void didUpdateWidget(AssistantAnswerMeta oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.message.reaction != widget.message.reaction) {
      _reaction = widget.message.reaction;
    }
  }

  Future<void> _copy() async {
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    // The assistant's answers are copied only while their source still
    // allows it.
    final authority = CopyAuthority.of(context);
    if (authority != null && !await authority.check(widget.text)) return;
    await Clipboard.setData(ClipboardData(text: widget.text));
    if (mounted) toast.info(i18n.t('chat:meta.copied'));
  }

  Future<void> _react(String value) async {
    final previous = _reaction;
    final next = previous == value ? null : value;
    setState(() {
      _reaction = next;
      _asking = next == 'down';
      _reason = null;
    });
    try {
      await ref
          .read(assistantApiProvider(widget.scope))
          .setReaction(widget.sessionId, widget.message.id, next);
    } catch (_) {
      if (mounted) {
        setState(() {
          _reaction = previous;
          _asking = false;
        });
      }
    }
  }

  /// Why it was turned down: the assistant learns how to talk from reasons
  /// that keep coming back.
  Future<void> _giveReason(String reason) async {
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    setState(() => _reason = reason);
    try {
      await ref
          .read(assistantApiProvider(widget.scope))
          .setReaction(
            widget.sessionId,
            widget.message.id,
            'down',
            reason: reason,
          );
      if (mounted) toast.success(i18n.t('chat:meta.reason.thanks'));
    } catch (_) {
      if (!mounted) return;
      setState(() => _reason = null);
      toast.error(i18n.t('chat:meta.reason.failed'));
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final time = messageTime(i18n, widget.message.createdAt);
    Widget action({
      required IconData icon,
      required String label,
      required VoidCallback? onTap,
      bool active = false,
    }) => IconButton(
      tooltip: label,
      onPressed: onTap,
      visualDensity: VisualDensity.compact,
      constraints: const BoxConstraints.tightFor(width: 32, height: 32),
      padding: EdgeInsets.zero,
      icon: Icon(icon, size: 15, color: active ? t.ink : t.n600),
    );
    final actions = Padding(
      padding: const EdgeInsets.only(top: 4),
      child: Row(
        children: [
          action(
            icon: Icons.copy_rounded,
            label: i18n.t('chat:meta.copyReply'),
            onTap: widget.text.trim().isEmpty ? null : _copy,
          ),
          action(
            icon: _reaction == 'up'
                ? Icons.thumb_up_alt
                : Icons.thumb_up_alt_outlined,
            label: i18n.t('chat:meta.likeReply'),
            active: _reaction == 'up',
            onTap: widget.streaming ? null : () => _react('up'),
          ),
          action(
            icon: _reaction == 'down'
                ? Icons.thumb_down_alt
                : Icons.thumb_down_alt_outlined,
            label: i18n.t('chat:meta.dislikeReply'),
            active: _reaction == 'down',
            onTap: widget.streaming ? null : () => _react('down'),
          ),
          if (time.isNotEmpty) ...[
            const SizedBox(width: 6),
            Text(
              time,
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
          ],
        ],
      ),
    );
    if (!_asking || _reaction != 'down') return actions;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        actions,
        Padding(
          padding: const EdgeInsets.only(top: 2, bottom: 4),
          child: Wrap(
            key: const ValueKey('reaction-reasons'),
            spacing: 6,
            runSpacing: 6,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              Text(
                i18n.t('chat:meta.reason.title'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
              for (final reason in reactionReasons)
                _ReasonChip(
                  key: ValueKey('reason-$reason'),
                  label: i18n.t('chat:meta.reason.$reason'),
                  picked: _reason == reason,
                  onTap: _reason == reason ? null : () => _giveReason(reason),
                ),
            ],
          ),
        ),
      ],
    );
  }
}

class _ReasonChip extends StatelessWidget {
  const _ReasonChip({
    super.key,
    required this.label,
    required this.picked,
    required this.onTap,
  });
  final String label;
  final bool picked;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Semantics(
      button: true,
      selected: picked,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(Radii.full),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(Radii.full),
            border: Border.all(color: picked ? t.ink : t.hair),
          ),
          child: Text(
            label,
            style: TextStyle(
              fontSize: FontSizes.xs,
              color: picked ? t.ink : t.n700,
            ),
          ),
        ),
      ),
    );
  }
}

/// The run ended without ever producing an answer, but it did work — say
/// so, rather than leaving a turn that looks like it had nothing to add.
class _IncompleteNotice extends ConsumerWidget {
  const _IncompleteNotice();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Container(
      margin: const EdgeInsets.only(top: 6),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
      decoration: BoxDecoration(
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.md),
        color: t.n100.withValues(alpha: 0.5),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            i18n.t('chat:final.missingTitle'),
            style: TextStyle(
              fontSize: FontSizes.sm,
              fontWeight: FontWeight.w500,
              color: t.n700,
            ),
          ),
          const SizedBox(height: 2),
          Text(
            i18n.t('chat:final.missingBody'),
            style: TextStyle(
              fontSize: FontSizes.xs,
              height: 1.6,
              color: t.n600,
            ),
          ),
        ],
      ),
    );
  }
}
