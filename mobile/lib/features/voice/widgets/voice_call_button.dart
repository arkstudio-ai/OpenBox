import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/api/assistant_profile.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../audio/mic_permission.dart';
import '../state/voice_call_controller.dart';
import '../voice_call_prepermission_page.dart';

/// The call entry in the assistant's top bar (mobile §2). Hidden when the
/// deployment has no voice calls ([enabled] false) — unless a call is
/// already on, which it then reopens.
class VoiceCallButton extends ConsumerWidget {
  const VoiceCallButton({super.key, required this.enabled});

  final bool enabled;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final call = ref.watch(voiceCallControllerProvider);
    if (!enabled && !call.active) return const SizedBox.shrink();
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return IconButton(
      key: const Key('voice-call-button'),
      tooltip: call.active
          ? i18n.t('voice:button.inCall')
          : i18n.t(
              'voice:button.startLabel',
              vars: {'name': assistantMention(ref)},
            ),
      icon: Icon(
        call.active ? Icons.phone_in_talk : Icons.phone_outlined,
        size: 20,
        color: call.active ? t.s700 : t.n700,
      ),
      onPressed: () => unawaited(openVoiceCall(context, ref)),
    );
  }
}

/// Opens the call page. With no call on, the microphone is explained first
/// when the system has not asked yet; "以后再说" stays where it is. The page
/// itself dials, so a second tap during a call only shows that call.
Future<void> openVoiceCall(BuildContext context, WidgetRef ref) async {
  final controller = ref.read(voiceCallControllerProvider.notifier);
  if (!ref.read(voiceCallControllerProvider).active) {
    controller.dismissEnded();
    final access = await controller.micAccess();
    if (!context.mounted) return;
    if (access == MicAccess.undetermined &&
        !await showVoicePrePermission(context)) {
      return;
    }
    if (!context.mounted) return;
    if (await controller.needsOverlayPermission()) {
      if (!context.mounted) return;
      final i18n = ref.read(i18nProvider);
      final enable = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: Text(i18n.t('voice:overlay.title')),
          content: Text(i18n.t('voice:overlay.body')),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: Text(i18n.t('voice:overlay.later')),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: Text(i18n.t('voice:permission.openSettings')),
            ),
          ],
        ),
      );
      if (enable == true) await controller.requestOverlayPermission();
      if (!context.mounted) return;
    }
  }
  if (context.mounted) unawaited(context.push(Paths.voice));
}
