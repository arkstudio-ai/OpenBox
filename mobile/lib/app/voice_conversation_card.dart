import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/chat/api/assistant_api.dart';
import '../features/chat/state/chat_session_controller.dart';
import '../features/chat/state/stream_store.dart';
import '../features/chat/utils/turn_view.dart';
import '../features/chat/widgets/assistant_task_card.dart';
import '../features/voice/state/voice_call_controller.dart';
import '../features/voice/state/voice_call_state.dart';
import '../shared/appearance/tokens.dart';
import '../shared/appearance/type_scale.dart';
import '../shared/i18n/i18n.dart';
import '../shared/models/message_part.dart';
import '../shared/models/session.dart';
import '../shared/router/paths.dart';

/// The call page's conversation card (VOICE_SEAM_PLAN §11.2): what was said,
/// as text. Two channels: the call itself (the `transcript` events) and, when
/// the server has named the task this call is about (`focus`), that task's
/// room: the latest exchanges of its execution conversation.
///
/// Lives in the app layer because it reads the voice call and the chat
/// stores at once; features do not import each other.
class VoiceConversationCard extends ConsumerStatefulWidget {
  const VoiceConversationCard({
    super.key,
    required this.scope,
    this.beforeOpen,
  });

  final AssistantScope scope;

  /// Collapses the call page before a room is opened.
  final VoidCallback? beforeOpen;

  @override
  ConsumerState<VoiceConversationCard> createState() =>
      _VoiceConversationCardState();
}

class _VoiceConversationCardState extends ConsumerState<VoiceConversationCard> {
  /// 0 = the call, 1 = the focused task's room.
  int _channel = 0;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final call = ref.watch(voiceCallControllerProvider);
    final focus = call.focus;
    String? roomSession = focus?.sessionId;
    if (focus != null && roomSession == null) {
      roomSession = ref
          .watch(assistantTaskProvider((scope: widget.scope, id: focus.taskId)))
          .asData
          ?.value
          .executionId;
    }
    final hasRoom = focus != null;
    final channel = hasRoom ? _channel : 0;
    return Container(
      key: const Key('voice-conversation-card'),
      margin: const EdgeInsets.symmetric(horizontal: 4),
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 10),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: t.hair),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            children: [
              _Tab(
                key: const Key('voice-conversation-channel-call'),
                label: i18n.t('voice:transcript.call'),
                selected: channel == 0,
                onTap: () => setState(() => _channel = 0),
              ),
              if (hasRoom) ...[
                const SizedBox(width: 6),
                Flexible(
                  child: _Tab(
                    key: const Key('voice-conversation-channel-room'),
                    label: i18n.t(
                      'voice:transcript.room',
                      vars: {'title': focus.title},
                    ),
                    selected: channel == 1,
                    onTap: () => setState(() => _channel = 1),
                  ),
                ),
              ],
            ],
          ),
          const SizedBox(height: 6),
          if (channel == 0)
            _CallLines(call: call, i18n: i18n)
          else if (roomSession == null || roomSession.isEmpty)
            Text(
              i18n.t('voice:transcript.taskEmpty'),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            )
          else
            _RoomLines(
              sessionId: roomSession,
              i18n: i18n,
              progress: focus?.progress ?? '',
              onOpen: () {
                widget.beforeOpen?.call();
                GoRouter.of(context).push(Paths.chat(roomSession!));
              },
            ),
        ],
      ),
    );
  }
}

class _Tab extends StatelessWidget {
  const _Tab({
    super.key,
    required this.label,
    required this.selected,
    required this.onTap,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(999),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 3),
        decoration: BoxDecoration(
          color: selected ? t.a100 : Colors.transparent,
          borderRadius: BorderRadius.circular(999),
        ),
        child: Text(
          label,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(
            fontSize: FontSizes.xs,
            fontWeight: selected ? FontWeight.w600 : FontWeight.w400,
            color: selected ? t.a700 : t.n700,
          ),
        ),
      ),
    );
  }
}

/// The call's own lines, newest at the bottom; the reply in progress in
/// italics, "在听你说…" while the user talks.
class _CallLines extends StatelessWidget {
  const _CallLines({required this.call, required this.i18n});

  final VoiceCallState call;
  final I18nState i18n;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final lines = call.transcript;
    final listening =
        call.phase == VoicePhase.listening &&
        call.hearing &&
        (lines.isEmpty || lines.last.isFinal);
    if (lines.isEmpty && !listening) {
      return Text(
        i18n.t('voice:transcript.empty'),
        style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
      );
    }
    final rows = <Widget>[
      for (final line in lines)
        _Line(
          who: i18n.t(
            line.role == 'user'
                ? 'voice:transcript.you'
                : 'voice:transcript.assistant',
          ),
          text: line.text,
          muted: line.role != 'user',
          italic: !line.isFinal,
        ),
      if (listening)
        Text(
          i18n.t('voice:transcript.hearing'),
          style: TextStyle(
            fontSize: FontSizes.xs,
            fontStyle: FontStyle.italic,
            color: t.n600,
          ),
        ),
    ];
    return ConstrainedBox(
      constraints: const BoxConstraints(maxHeight: 180),
      child: ListView(
        shrinkWrap: true,
        reverse: true,
        padding: EdgeInsets.zero,
        children: rows.reversed.toList(),
      ),
    );
  }
}

/// The focused task's room: the last few exchanges of its execution
/// conversation, what it is doing now, and the way in.
class _RoomLines extends ConsumerWidget {
  const _RoomLines({
    required this.sessionId,
    required this.i18n,
    required this.onOpen,
    this.progress = '',
  });

  final String sessionId;
  final I18nState i18n;
  final VoidCallback onOpen;

  /// The server's own word on what the room is doing now (`focus.progress`).
  final String progress;

  static const _recentRows = 8;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final session = ref.watch(chatSessionProvider(sessionId));
    final messages = ref.watch(
      chatStreamProvider.select((s) => s.messagesOf(sessionId)),
    );
    final status =
        ref.watch(chatStreamProvider.select((s) => s.statusOf(sessionId))) ??
        session.session?.status;
    final rows = buildChatRows(messages);
    final recent = rows.length > _recentRows
        ? rows.sublist(rows.length - _recentRows)
        : rows;
    final lines = <Widget>[
      for (final row in recent)
        switch (row) {
          UserRowData(:final message) => _Line(
            who: i18n.t('voice:transcript.you'),
            text: message.parts
                .whereType<TextPart>()
                .map((part) => part.text)
                .join('\n')
                .trim(),
            muted: false,
          ),
          AssistantTurnData(:final bodyText) => _Line(
            who: i18n.t('voice:transcript.assistant'),
            text: bodyText.trim(),
            muted: true,
          ),
        },
    ].where((line) => line is! _Line || line.text.isNotEmpty).toList();
    final note = progress.isNotEmpty
        ? i18n.t('voice:transcript.taskDoing', vars: {'step': progress})
        : isBusyStatus(status)
        ? i18n.t('voice:transcript.taskBusy')
        : status == SessionStatus.waitingInput
        ? i18n.t('voice:transcript.taskWaiting')
        : null;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        if (lines.isEmpty && !session.loading)
          Text(
            i18n.t('voice:transcript.taskEmpty'),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
          )
        else
          ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 160),
            child: ListView(
              shrinkWrap: true,
              reverse: true,
              padding: EdgeInsets.zero,
              children: lines.reversed.toList(),
            ),
          ),
        if (note != null)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text(
              note,
              style: TextStyle(
                fontSize: FontSizes.xs,
                fontStyle: FontStyle.italic,
                color: t.n600,
              ),
            ),
          ),
        Align(
          alignment: Alignment.centerRight,
          child: TextButton(
            key: const Key('voice-conversation-open-room'),
            onPressed: onOpen,
            style: TextButton.styleFrom(visualDensity: VisualDensity.compact),
            child: Text(
              i18n.t('voice:transcript.openRoom'),
              style: TextStyle(fontSize: FontSizes.xs, color: t.a700),
            ),
          ),
        ),
      ],
    );
  }
}

class _Line extends StatelessWidget {
  const _Line({
    required this.who,
    required this.text,
    required this.muted,
    this.italic = false,
  });

  final String who;
  final String text;
  final bool muted;
  final bool italic;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.only(bottom: 3),
      child: Text.rich(
        TextSpan(
          children: [
            TextSpan(
              text: '$who  ',
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
            TextSpan(text: text),
          ],
        ),
        style: TextStyle(
          fontSize: FontSizes.sm,
          height: 1.3,
          fontStyle: italic ? FontStyle.italic : FontStyle.normal,
          color: muted ? t.n800 : t.ink,
        ),
      ),
    );
  }
}
