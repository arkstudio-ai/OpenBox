import 'dart:async';
import 'dart:ui';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/assistant_profile.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../state/voice_call_controller.dart';
import '../state/voice_call_state.dart';
import 'voice_copy.dart';
import 'voice_cost_sheet.dart';
import 'voice_ended_panel.dart';

/// The call sheet under the call bar (VOICE_SEAM_PLAN §13): half the screen,
/// translucent over whatever page is open, holding the call's words as text
/// — the assistant's lines on the left under the name the person gave it
/// (the helper's while watching a task's room), the person's own on the
/// right under "我". Mute and hang-up stay on the bar above, so the foot
/// carries only what the bar has not: the cost so far and the speaker /
/// earpiece switch (resume, while paused). A finished call shows its
/// summary here instead. Swiping up folds it into the bar.
class VoiceCallSheet extends ConsumerWidget {
  const VoiceCallSheet({super.key});

  /// Share of the screen height the sheet takes.
  static const fraction = 0.5;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final call = ref.watch(voiceCallControllerProvider);
    final controller = ref.read(voiceCallControllerProvider.notifier);
    final end = call.status == VoiceCallStatus.ended ? call.end : null;
    final cost = end?.cost ?? call.cost;
    final height = MediaQuery.sizeOf(context).height * fraction;
    final dark = Theme.of(context).brightness == Brightness.dark;
    return GestureDetector(
      key: const Key('voice-sheet'),
      behavior: HitTestBehavior.opaque,
      // A firm upward swipe folds the sheet, like the system's own.
      onVerticalDragEnd: (details) {
        if ((details.primaryVelocity ?? 0) < -500) {
          controller.setExpanded(false);
        }
      },
      child: ClipRRect(
        borderRadius: const BorderRadius.vertical(bottom: Radius.circular(20)),
        child: BackdropFilter(
          filter: ImageFilter.blur(sigmaX: 8, sigmaY: 8),
          child: Container(
            height: height,
            decoration: BoxDecoration(
              color: t.bg.withValues(alpha: dark ? 0.86 : 0.9),
              border: Border(bottom: BorderSide(color: t.hair)),
            ),
            padding: const EdgeInsets.fromLTRB(16, 10, 16, 6),
            child: Column(
              children: [
                Expanded(
                  child: end != null
                      ? Column(
                          children: [
                            Text(
                              VoiceCopy.status(i18n, call),
                              key: const Key('voice-status'),
                              textAlign: TextAlign.center,
                              style: TextStyle(
                                fontSize: FontSizes.base,
                                fontWeight: FontWeight.w500,
                                color: t.ink,
                              ),
                            ),
                            const SizedBox(height: 8),
                            Expanded(
                              child: VoiceEndedPanel(
                                end: end,
                                onClose: () => controller.setExpanded(false),
                              ),
                            ),
                          ],
                        )
                      : _Transcript(call: call, i18n: i18n),
                ),
                if (end == null) _Foot(call: call, cost: cost, i18n: i18n),
                _Handle(onTap: () => controller.setExpanded(false)),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// The call as text, newest at the bottom; the reply being spoken in
/// italics, "在听你说…" while the person is being heard.
class _Transcript extends ConsumerWidget {
  const _Transcript({required this.call, required this.i18n});

  final VoiceCallState call;
  final I18nState i18n;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final lines = call.transcript;
    final listening =
        call.phase == VoicePhase.listening &&
        call.hearing &&
        (lines.isEmpty || lines.last.isFinal);
    if (lines.isEmpty && !listening) {
      return Center(
        child: Text(
          i18n.t('voice:transcript.empty'),
          key: const Key('voice-transcript-empty'),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
        ),
      );
    }
    final assistant = assistantLabel(ref);
    final rows = <Widget>[
      for (final line in lines)
        _Line(
          key: ValueKey('voice-line:${line.role}:${line.itemId}'),
          who: line.role == 'user'
              ? i18n.t('voice:transcript.you')
              : line.room
              ? i18n.t('voice:transcript.helper')
              : assistant,
          text: line.text,
          mine: line.role == 'user',
          italic: !line.isFinal,
        ),
      if (listening)
        Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Text(
            i18n.t('voice:transcript.hearing'),
            key: const Key('voice-transcript-hearing'),
            textAlign: TextAlign.right,
            style: TextStyle(
              fontSize: FontSizes.sm,
              fontStyle: FontStyle.italic,
              color: t.n600,
            ),
          ),
        ),
    ];
    return ListView(
      key: const Key('voice-transcript'),
      reverse: true,
      padding: EdgeInsets.zero,
      children: rows.reversed.toList(),
    );
  }
}

/// One thing said: the speaker on its own small line, the words under it;
/// the person's own on the right.
class _Line extends StatelessWidget {
  const _Line({
    super.key,
    required this.who,
    required this.text,
    required this.mine,
    required this.italic,
  });

  final String who;
  final String text;
  final bool mine;
  final bool italic;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final align = mine ? CrossAxisAlignment.end : CrossAxisAlignment.start;
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Column(
        crossAxisAlignment: align,
        children: [
          Text(
            who,
            style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
          ),
          const SizedBox(height: 2),
          Padding(
            padding: EdgeInsets.only(left: mine ? 40 : 0, right: mine ? 0 : 40),
            child: Text(
              text,
              textAlign: mine ? TextAlign.right : TextAlign.left,
              style: TextStyle(
                fontSize: FontSizes.base,
                height: 1.45,
                fontStyle: italic ? FontStyle.italic : FontStyle.normal,
                color: t.ink,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// The sheet's last line: the cost so far on the left, on the right the
/// one control the bar lacks — speaker or earpiece, or 恢复通话 while the
/// call is paused by an interruption.
class _Foot extends ConsumerWidget {
  const _Foot({required this.call, required this.cost, required this.i18n});

  final VoiceCallState call;
  final VoiceCost? cost;
  final I18nState i18n;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final controller = ref.read(voiceCallControllerProvider.notifier);
    final paused = call.status == VoiceCallStatus.paused;
    final connected = call.status == VoiceCallStatus.connected;
    final Widget? action = paused
        ? _FootButton(
            key: const Key('voice-resume'),
            icon: Icons.play_arrow,
            label: i18n.t('voice:controls.resume'),
            accent: true,
            onTap: () => unawaited(controller.resumeAudio()),
          )
        : connected
        ? _FootButton(
            key: const Key('voice-speaker'),
            icon: call.speakerOn ? Icons.volume_up : Icons.hearing,
            label: i18n.t(
              call.speakerOn
                  ? 'voice:controls.speaker'
                  : 'voice:controls.earpiece',
            ),
            toggled: call.speakerOn,
            onTap: () => unawaited(controller.toggleSpeaker()),
          )
        : null;
    return SizedBox(
      height: 36,
      child: Row(
        children: [
          Expanded(
            child: cost == null
                ? const SizedBox.shrink()
                : Align(
                    alignment: Alignment.centerLeft,
                    child: VoiceCostLine(cost: cost!),
                  ),
          ),
          if (action != null) action,
        ],
      ),
    );
  }
}

/// A small icon-and-word button for the foot.
class _FootButton extends StatelessWidget {
  const _FootButton({
    super.key,
    required this.icon,
    required this.label,
    required this.onTap,
    this.toggled,
    this.accent = false,
  });

  final IconData icon;
  final String label;
  final VoidCallback onTap;
  final bool? toggled;
  final bool accent;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final on = toggled ?? false;
    final color = accent ? t.a700 : t.ink;
    return Semantics(
      button: true,
      toggled: toggled,
      label: label,
      excludeSemantics: true,
      onTap: onTap,
      child: Material(
        color: on ? t.ink : t.n200,
        borderRadius: BorderRadius.circular(18),
        child: InkWell(
          borderRadius: BorderRadius.circular(18),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(icon, size: 18, color: on ? t.bg : color),
                const SizedBox(width: 6),
                Text(
                  label,
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w500,
                    color: on ? t.bg : color,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// The grab bar at the bottom edge; tapping it folds the sheet too.
class _Handle extends StatelessWidget {
  const _Handle({required this.onTap});

  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return GestureDetector(
      key: const Key('voice-sheet-handle'),
      behavior: HitTestBehavior.opaque,
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 6),
        child: Center(
          child: Container(
            width: 40,
            height: 4,
            decoration: BoxDecoration(
              color: t.n300,
              borderRadius: BorderRadius.circular(2),
            ),
          ),
        ),
      ),
    );
  }
}

/// The 盯现场 switch on the focused task's card while a call is on
/// (VOICE_SEAM_PLAN §13.1): on, what the person says goes straight to this
/// task and its replies are read out; the server turns it off when the work
/// is done. Nothing when there is no call, or the card is another task's.
class VoiceRoomSwitch extends ConsumerWidget {
  const VoiceRoomSwitch({super.key, required this.taskId});

  final String taskId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final focus = ref.watch(
      voiceCallControllerProvider.select(
        (s) => s.status == VoiceCallStatus.connected ? s.focus : null,
      ),
    );
    if (focus == null || focus.taskId != taskId) return const SizedBox.shrink();
    final on = focus.bound;
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  i18n.t(
                    on ? 'voice:transcript.watchOn' : 'voice:transcript.watch',
                  ),
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w500,
                    color: on ? t.a700 : t.ink,
                  ),
                ),
                Text(
                  i18n.t('voice:transcript.watchHint'),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
              ],
            ),
          ),
          Switch.adaptive(
            key: const Key('voice-room-switch'),
            value: on,
            onChanged: (value) => unawaited(
              Future.sync(
                () => ref
                    .read(voiceCallControllerProvider.notifier)
                    .setRoom(value ? taskId : null),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
