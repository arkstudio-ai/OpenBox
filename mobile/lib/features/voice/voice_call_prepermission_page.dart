import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/api/assistant_profile.dart';
import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';

/// Explains the microphone before the system asks for it (mobile §4, same
/// shape as the notification pre-permission page). True when the person
/// chose "允许" — the caller then dials and the system dialog follows.
Future<bool> showVoicePrePermission(BuildContext context) async {
  final allowed = await Navigator.of(context, rootNavigator: true).push<bool>(
    MaterialPageRoute<bool>(
      fullscreenDialog: true,
      builder: (_) => const VoicePrepermissionPage(),
    ),
  );
  return allowed ?? false;
}

class VoicePrepermissionPage extends ConsumerWidget {
  const VoicePrepermissionPage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Scaffold(
      backgroundColor: t.bg,
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(28, 0, 28, 18),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Spacer(),
              Container(
                width: 72,
                height: 72,
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: t.s100,
                  borderRadius: BorderRadius.circular(Radii.xl2),
                ),
                child: Icon(Icons.mic_none, size: 32, color: t.s700),
              ),
              const SizedBox(height: 22),
              Text(
                i18n.t('voice:permission.title'),
                style: TextStyle(
                  fontSize: FontSizes.xl3,
                  fontWeight: FontWeight.w700,
                  height: 1.25,
                  color: t.ink,
                ),
              ),
              const SizedBox(height: 12),
              Text(
                i18n.t(
                  'voice:permission.body',
                  vars: {'name': assistantMention(ref)},
                ),
                style: TextStyle(
                  fontSize: FontSizes.base,
                  height: 1.65,
                  color: t.n700,
                ),
              ),
              const Spacer(flex: 2),
              FilledButton(
                key: const Key('voice-permission-allow'),
                onPressed: () => Navigator.of(context).pop(true),
                style: FilledButton.styleFrom(
                  backgroundColor: t.a700,
                  foregroundColor: t.bg,
                  minimumSize: const Size.fromHeight(52),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                ),
                child: Text(
                  i18n.t('voice:permission.allow'),
                  style: const TextStyle(
                    fontSize: FontSizes.lg,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              const SizedBox(height: 6),
              TextButton(
                key: const Key('voice-permission-later'),
                onPressed: () => Navigator.of(context).pop(false),
                style: TextButton.styleFrom(
                  minimumSize: const Size.fromHeight(44),
                ),
                child: Text(
                  i18n.t('voice:permission.later'),
                  style: TextStyle(fontSize: FontSizes.base, color: t.n700),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
