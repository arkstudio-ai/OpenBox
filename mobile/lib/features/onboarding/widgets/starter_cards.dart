import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../store/models/store.dart';
import '../../store/state/store_provider.dart';
import '../state/onboarding_store.dart';

const _defaultIndustry = 'food';

/// L2: industry starter cards that replace the generic suggestions on the
/// empty chat until the account's first send.
///
/// With a registered store the cards come from `GET /api/stores/{id}/
/// starter-cards` (docs/OPS_CASE_PLAN.md §2.4) and follow its industry;
/// until they arrive (or if they fail) the locale cards for that industry
/// stand in. Without a store the industry is the one picked before the
/// store step existed, else food.
class StarterCards extends ConsumerWidget {
  const StarterCards({super.key, required this.onPick});

  final ValueChanged<String> onPick;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final store = ref.watch(
      storeProvider.select((s) => s.valueOrNull?.store),
    );
    final industry =
        store?.starterIndustry ??
        ref.watch(onboardingProvider.select((s) => s.industry)) ??
        _defaultIndustry;
    var cards = _localeCards(i18n, industry);
    if (cards.isEmpty) cards = _localeCards(i18n, _defaultIndustry);
    if (store != null) {
      final fromApi = ref.watch(storeStarterCardsProvider).valueOrNull;
      if (fromApi != null && fromApi.isNotEmpty) cards = fromApi;
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          i18n.t('onboarding:starter.label'),
          style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
        ),
        const SizedBox(height: 8),
        for (final card in cards)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Material(
              color: t.card,
              borderRadius: BorderRadius.circular(Radii.lg),
              child: InkWell(
                borderRadius: BorderRadius.circular(Radii.lg),
                onTap: () => onPick(card.title),
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
                              card.title,
                              style: TextStyle(
                                fontSize: FontSizes.base,
                                color: t.ink,
                              ),
                            ),
                            const SizedBox(height: 2),
                            Text(
                              card.hint,
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

  static List<StarterCard> _localeCards(I18nState i18n, String industry) => [
    for (final card in i18n.tList('onboarding:starter.cards.$industry'))
      if (card is Map<String, dynamic>) StarterCard.fromJson(card),
  ];
}
