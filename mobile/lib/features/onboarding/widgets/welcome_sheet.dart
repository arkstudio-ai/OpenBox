import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../state/onboarding_store.dart';

/// L2: one-screen welcome sheet on the account's first empty chat.
/// Returns true when it was shown.
Future<bool> showWelcomeSheet(
  BuildContext context,
  WidgetRef ref, {
  required String name,
}) async {
  final onboarding = ref.read(onboardingProvider.notifier);
  if (!onboarding.shouldShow(Guides.welcome)) return false;
  final queue = ref.read(guideQueueProvider.notifier);
  if (!queue.claim(Guides.welcome)) return false;
  unawaited(onboarding.markSeen(Guides.welcome));
  try {
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: Colors.transparent,
      builder: (_) => _WelcomeSheet(name: name),
    );
  } finally {
    queue.release(Guides.welcome);
  }
  return true;
}

class _WelcomeSheet extends ConsumerWidget {
  const _WelcomeSheet({required this.name});

  final String name;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final steps = i18n.tList('onboarding:welcome.steps');
    return Container(
      padding: EdgeInsets.fromLTRB(
        24,
        12,
        24,
        24 + MediaQuery.paddingOf(context).bottom,
      ),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: const BorderRadius.vertical(top: Radius.circular(24)),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Center(
            child: Container(
              width: 36,
              height: 4,
              decoration: BoxDecoration(
                color: t.n400,
                borderRadius: BorderRadius.circular(Radii.full),
              ),
            ),
          ),
          const SizedBox(height: 18),
          Row(
            children: [
              ClipRRect(
                borderRadius: BorderRadius.circular(Radii.xl),
                child: Image.asset(
                  'assets/onboarding/welcome.jpg',
                  width: 84,
                  height: 84,
                  fit: BoxFit.cover,
                  errorBuilder: (_, _, _) =>
                      SizedBox(width: 84, height: 84, child: ColoredBox(color: t.surface)),
                ),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      i18n.t('onboarding:welcome.title', vars: {'name': name}),
                      style: TextStyle(
                        fontSize: FontSizes.xl2,
                        fontWeight: FontWeight.w700,
                        height: 1.3,
                        color: t.ink,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      i18n.t('onboarding:welcome.lede'),
                      style: TextStyle(fontSize: FontSizes.md, color: t.n700),
                    ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 20),
          for (var i = 0; i < steps.length; i++) ...[
            if (i > 0) const SizedBox(height: 14),
            _Step(index: i + 1, data: steps[i], tokens: t),
          ],
          const SizedBox(height: 22),
          FilledButton(
            key: const Key('welcome-start'),
            onPressed: () => Navigator.of(context).pop(),
            style: FilledButton.styleFrom(
              backgroundColor: t.a700,
              foregroundColor: t.bg,
              minimumSize: const Size.fromHeight(52),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(Radii.full),
              ),
            ),
            child: Text(
              i18n.t('onboarding:welcome.start'),
              style: const TextStyle(
                fontSize: FontSizes.lg,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _Step extends StatelessWidget {
  const _Step({required this.index, required this.data, required this.tokens});

  final int index;
  final dynamic data;
  final BossipTokens tokens;

  @override
  Widget build(BuildContext context) {
    final t = tokens;
    final map = data is Map ? data as Map : const <String, Object?>{};
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Container(
          width: 26,
          height: 26,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: t.a100,
            borderRadius: BorderRadius.circular(Radii.full),
          ),
          child: Text(
            '$index',
            style: TextStyle(
              fontSize: FontSizes.sm,
              fontWeight: FontWeight.w700,
              color: t.accent,
            ),
          ),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                '${map['title'] ?? ''}',
                style: TextStyle(
                  fontSize: FontSizes.base,
                  fontWeight: FontWeight.w600,
                  color: t.ink,
                ),
              ),
              const SizedBox(height: 2),
              Text(
                '${map['body'] ?? ''}',
                style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
              ),
            ],
          ),
        ),
      ],
    );
  }
}
