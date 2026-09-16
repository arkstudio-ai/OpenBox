import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../state/onboarding_store.dart';

/// L3: explains notifications before the OS dialog. Resolves true when the
/// person chose to enable (the caller then asks the OS), false on "later".
/// Returns null when it did not show (already seen, or a guide is busy).
Future<bool?> showNotifyPrePermission(BuildContext context, WidgetRef ref) async {
  final onboarding = ref.read(onboardingProvider.notifier);
  if (!onboarding.shouldShow(Guides.notifyPrePermission)) return null;
  final queue = ref.read(guideQueueProvider.notifier);
  if (!queue.claim(Guides.notifyPrePermission)) return null;
  unawaited(onboarding.markSeen(Guides.notifyPrePermission));
  try {
    final result = await Navigator.of(context, rootNavigator: true).push<bool>(
      MaterialPageRoute<bool>(
        fullscreenDialog: true,
        builder: (_) => const _NotifyPrePermissionPage(),
      ),
    );
    return result ?? false;
  } finally {
    queue.release(Guides.notifyPrePermission);
  }
}

class _NotifyPrePermissionPage extends ConsumerWidget {
  const _NotifyPrePermissionPage();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final points = i18n.tList('onboarding:notify.points');
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
                  color: t.a100,
                  borderRadius: BorderRadius.circular(Radii.xl2),
                ),
                child: Icon(
                  Icons.notifications_none,
                  size: 32,
                  color: t.accent,
                ),
              ),
              const SizedBox(height: 22),
              Text(
                i18n.t('onboarding:notify.title'),
                style: TextStyle(
                  fontSize: FontSizes.xl3,
                  fontWeight: FontWeight.w700,
                  height: 1.25,
                  color: t.ink,
                ),
              ),
              const SizedBox(height: 12),
              Text(
                i18n.t('onboarding:notify.body'),
                style: TextStyle(
                  fontSize: FontSizes.base,
                  height: 1.65,
                  color: t.n700,
                ),
              ),
              const SizedBox(height: 20),
              for (final point in points)
                Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: Row(
                    children: [
                      Icon(Icons.check, size: 16, color: t.sage),
                      const SizedBox(width: 10),
                      Expanded(
                        child: Text(
                          '$point',
                          style: TextStyle(
                            fontSize: FontSizes.md,
                            color: t.n800,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              const Spacer(flex: 2),
              FilledButton(
                key: const Key('notify-enable'),
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
                  i18n.t('onboarding:notify.enable'),
                  style: const TextStyle(
                    fontSize: FontSizes.lg,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              const SizedBox(height: 6),
              TextButton(
                key: const Key('notify-later'),
                onPressed: () => Navigator.of(context).pop(false),
                style: TextButton.styleFrom(
                  minimumSize: const Size.fromHeight(44),
                ),
                child: Text(
                  i18n.t('onboarding:notify.later'),
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
