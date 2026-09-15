import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../state/onboarding_store.dart';

/// M2: a one-time explainer strip keyed on a guide. Renders nothing once the
/// account has dismissed it (or before the server has answered).
class FirstSeenHint extends ConsumerWidget {
  const FirstSeenHint({
    super.key,
    required this.guide,
    required this.title,
    required this.body,
    this.margin = const EdgeInsets.only(bottom: 10),
    this.compact = false,
  });

  final String guide;
  final String title;
  final String body;
  final EdgeInsets margin;

  /// Inside chat cards: tighter paddings and smaller type.
  final bool compact;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final show = ref.watch(
      onboardingProvider.select((s) => s.loaded && !s.seen(guide)),
    );
    if (!show) return const SizedBox.shrink();
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Container(
      key: Key('hint-$guide'),
      margin: margin,
      padding: compact
          ? const EdgeInsets.fromLTRB(12, 10, 8, 4)
          : const EdgeInsets.fromLTRB(16, 14, 10, 6),
      decoration: BoxDecoration(
        color: t.a100,
        borderRadius: BorderRadius.circular(compact ? Radii.md : Radii.lg),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            title,
            style: TextStyle(
              fontSize: compact ? FontSizes.sm : FontSizes.base,
              fontWeight: FontWeight.w600,
              color: t.ink,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            body,
            style: TextStyle(
              fontSize: compact ? FontSizes.xs : FontSizes.sm,
              height: 1.55,
              color: t.n800,
            ),
          ),
          Align(
            alignment: Alignment.centerRight,
            child: TextButton(
              onPressed: () =>
                  ref.read(onboardingProvider.notifier).markSeen(guide),
              style: TextButton.styleFrom(
                minimumSize: const Size(0, 32),
                padding: const EdgeInsets.symmetric(horizontal: 10),
              ),
              child: Text(
                i18n.t('onboarding:m2.cards.done'),
                style: TextStyle(fontSize: FontSizes.sm, color: t.a800),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
