import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../state/voice_call_controller.dart';
import '../state/voice_call_state.dart';
import 'voice_copy.dart';

/// Reasons a second try can fix from the phone itself.
const _retryable = {
  VoiceEndReason.micDenied,
  VoiceEndReason.micMissing,
  VoiceEndReason.micBusy,
  VoiceEndReason.micLost,
};

/// The page's end state (mobile §3): why, how long, what it cost, what is
/// still being worked on, and what can be done next. The headline itself is
/// the page's state line.
class VoiceEndedPanel extends ConsumerWidget {
  const VoiceEndedPanel({super.key, required this.end, required this.onClose});

  final VoiceCallEnd end;
  final VoidCallback onClose;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final controller = ref.read(voiceCallControllerProvider.notifier);
    final lines = [
      VoiceCopy.endDetail(i18n, end),
      VoiceCopy.endFigures(i18n, end),
      VoiceCopy.pendingHint(i18n, end),
    ].whereType<String>();
    final again = end.canRedial
        ? i18n.t('voice:actions.redial')
        : _retryable.contains(end.reason)
        ? i18n.t('voice:actions.retry')
        : null;
    final buttonShape = RoundedRectangleBorder(
      borderRadius: BorderRadius.circular(Radii.full),
    );
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        for (final line in lines)
          Padding(
            padding: const EdgeInsets.only(bottom: 6),
            child: Text(
              line,
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: FontSizes.md,
                height: 1.5,
                color: t.n700,
              ),
            ),
          ),
        const SizedBox(height: 18),
        if (end.reason == VoiceEndReason.micDenied) ...[
          FilledButton(
            key: const Key('voice-open-settings'),
            onPressed: () => unawaited(controller.openMicSettings()),
            style: FilledButton.styleFrom(
              backgroundColor: t.a700,
              foregroundColor: t.bg,
              minimumSize: const Size.fromHeight(48),
              shape: buttonShape,
            ),
            child: Text(i18n.t('voice:permission.openSettings')),
          ),
          const SizedBox(height: 8),
        ],
        // Calls are paid in credits: out of them, the way on is a top-up.
        if (end.reason == VoiceEndReason.quota) ...[
          FilledButton(
            key: const Key('voice-top-up'),
            onPressed: () {
              final router = GoRouter.of(context);
              onClose();
              unawaited(router.push(Paths.billing()));
            },
            style: FilledButton.styleFrom(
              backgroundColor: t.s700,
              foregroundColor: Colors.white,
              minimumSize: const Size.fromHeight(48),
              shape: buttonShape,
            ),
            child: Text(i18n.t('voice:ended.topUp')),
          ),
          const SizedBox(height: 8),
        ],
        if (again != null) ...[
          FilledButton(
            key: const Key('voice-redial'),
            onPressed: () => unawaited(controller.redial()),
            style: FilledButton.styleFrom(
              backgroundColor: end.reason == VoiceEndReason.micDenied
                  ? t.n200
                  : t.s700,
              foregroundColor: end.reason == VoiceEndReason.micDenied
                  ? t.ink
                  : Colors.white,
              minimumSize: const Size.fromHeight(48),
              shape: buttonShape,
            ),
            child: Text(again),
          ),
          const SizedBox(height: 8),
        ],
        TextButton(
          key: const Key('voice-close'),
          onPressed: onClose,
          style: TextButton.styleFrom(minimumSize: const Size.fromHeight(44)),
          child: Text(
            i18n.t('voice:actions.close'),
            style: TextStyle(fontSize: FontSizes.base, color: t.n700),
          ),
        ),
      ],
    );
  }
}
