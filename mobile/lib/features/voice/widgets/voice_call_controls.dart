import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../state/voice_call_controller.dart';
import '../state/voice_call_state.dart';

/// The row of round buttons under the orb (mobile §3): mute · hang up ·
/// speaker while connected, a grey cancel while dialling, hang-up alone
/// while paused, everything inert while hanging up.
class VoiceCallControls extends ConsumerWidget {
  const VoiceCallControls({super.key, required this.call, this.onHangUp});

  final VoiceCallState call;

  /// Called after hang-up (or cancel) is pressed; the page pops itself.
  final VoidCallback? onHangUp;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final controller = ref.read(voiceCallControllerProvider.notifier);
    void hangUp() {
      onHangUp?.call();
      controller.hangUp();
    }

    // Idle only for the moment a cancelled page takes to slide away.
    final cancel = call.dialling || call.status == VoiceCallStatus.idle;
    final hangUpButton = VoiceRoundButton(
      key: const Key('voice-hang-up'),
      size: 72,
      icon: Icons.call_end,
      label: i18n.t(cancel ? 'voice:controls.cancel' : 'voice:controls.hangUp'),
      background: cancel ? t.n500 : t.danger,
      foreground: Colors.white,
      onPressed: call.status == VoiceCallStatus.ending ? null : hangUp,
    );
    if (call.status != VoiceCallStatus.connected) {
      return Center(child: hangUpButton);
    }
    return Row(
      mainAxisAlignment: MainAxisAlignment.spaceEvenly,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        VoiceRoundButton(
          key: const Key('voice-mute'),
          icon: call.muted ? Icons.mic_off : Icons.mic_none,
          label: i18n.t(
            call.muted ? 'voice:controls.unmute' : 'voice:controls.mute',
          ),
          background: call.muted ? t.ink : t.n200,
          foreground: call.muted ? t.bg : t.ink,
          selected: call.muted,
          onPressed: controller.toggleMute,
        ),
        hangUpButton,
        VoiceRoundButton(
          key: const Key('voice-speaker'),
          icon: call.speakerOn ? Icons.volume_up : Icons.hearing,
          label: i18n.t(
            call.speakerOn
                ? 'voice:controls.speaker'
                : 'voice:controls.earpiece',
          ),
          background: call.speakerOn ? t.ink : t.n200,
          foreground: call.speakerOn ? t.bg : t.ink,
          selected: call.speakerOn,
          onPressed: controller.toggleSpeaker,
        ),
      ],
    );
  }
}

/// A phone-style round button with its label underneath.
class VoiceRoundButton extends StatelessWidget {
  const VoiceRoundButton({
    super.key,
    required this.icon,
    required this.label,
    required this.background,
    required this.foreground,
    required this.onPressed,
    this.size = 56,
    this.selected,
  });

  final IconData icon;
  final String label;
  final Color background;
  final Color foreground;
  final VoidCallback? onPressed;
  final double size;

  /// For toggles: announced as on/off.
  final bool? selected;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final enabled = onPressed != null;
    return Semantics(
      button: true,
      enabled: enabled,
      toggled: selected,
      label: label,
      excludeSemantics: true,
      onTap: onPressed,
      // The label is part of the target: a thumb aimed at "挂断" counts.
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTap: onPressed,
        child: Opacity(
          opacity: enabled ? 1 : 0.45,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Material(
                color: background,
                shape: const CircleBorder(),
                clipBehavior: Clip.antiAlias,
                child: InkWell(
                  onTap: onPressed,
                  child: SizedBox.square(
                    dimension: size,
                    child: Icon(icon, color: foreground, size: size * 0.42),
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Text(
                label,
                style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
