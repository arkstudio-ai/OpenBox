import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../state/onboarding_store.dart';

const starterIndustries = ['beauty', 'food', 'retail'];

/// L2: industry starter cards that replace the generic suggestions on the
/// empty chat until the account's first send.
class StarterCards extends ConsumerWidget {
  const StarterCards({super.key, required this.onPick});

  final ValueChanged<String> onPick;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final industry =
        ref.watch(onboardingProvider.select((s) => s.industry)) ??
        starterIndustries.first;
    final cards = i18n.tList('onboarding:starter.cards.$industry');
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Text(
              i18n.t('onboarding:starter.label'),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
            ),
            const Spacer(),
            for (final id in starterIndustries)
              Padding(
                padding: const EdgeInsets.only(left: 6),
                child: _IndustryChip(
                  key: Key('industry-$id'),
                  label: i18n.t('onboarding:starter.industry.$id'),
                  active: id == industry,
                  onTap: () =>
                      ref.read(onboardingProvider.notifier).setIndustry(id),
                  tokens: t,
                ),
              ),
          ],
        ),
        const SizedBox(height: 8),
        for (final card in cards)
          if (card is Map)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Material(
                color: t.card,
                borderRadius: BorderRadius.circular(Radii.lg),
                child: InkWell(
                  borderRadius: BorderRadius.circular(Radii.lg),
                  onTap: () => onPick('${card['title'] ?? ''}'),
                  child: Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 16,
                      vertical: 14,
                    ),
                    decoration: BoxDecoration(
                      border: Border.all(color: t.hair),
                      borderRadius: BorderRadius.circular(Radii.lg),
                    ),
                    child: Row(
                      children: [
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                '${card['title'] ?? ''}',
                                style: TextStyle(
                                  fontSize: FontSizes.base,
                                  color: t.ink,
                                ),
                              ),
                              const SizedBox(height: 2),
                              Text(
                                '${card['hint'] ?? ''}',
                                style: TextStyle(
                                  fontSize: FontSizes.xs,
                                  color: t.n600,
                                ),
                              ),
                            ],
                          ),
                        ),
                        Icon(Icons.arrow_forward, size: 18, color: t.n600),
                      ],
                    ),
                  ),
                ),
              ),
            ),
      ],
    );
  }
}

class _IndustryChip extends StatelessWidget {
  const _IndustryChip({
    super.key,
    required this.label,
    required this.active,
    required this.onTap,
    required this.tokens,
  });

  final String label;
  final bool active;
  final VoidCallback onTap;
  final BossipTokens tokens;

  @override
  Widget build(BuildContext context) {
    final t = tokens;
    return InkWell(
      borderRadius: BorderRadius.circular(Radii.full),
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
        decoration: BoxDecoration(
          color: active ? t.a700 : Colors.transparent,
          border: Border.all(color: active ? t.a700 : t.hair),
          borderRadius: BorderRadius.circular(Radii.full),
        ),
        child: Text(
          label,
          style: TextStyle(
            fontSize: FontSizes.xs,
            color: active ? t.bg : t.n700,
          ),
        ),
      ),
    );
  }
}
