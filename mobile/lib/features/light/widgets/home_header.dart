import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/utils/format.dart';
import '../../billing/state/billing_providers.dart';
import '../../store/state/store_provider.dart';

/// Top of the Light home (docs `LIGHT_MODE_PLAN.md` §4.2): the shop (or the
/// person) by name, a greeting, and the credit balance — which
/// opens the plans and usage page.
class LightHomeHeader extends ConsumerWidget {
  const LightHomeHeader({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final user = ref.watch(authProvider).user;
    final shop = ref.watch(storeProvider).valueOrNull?.name;
    final credits = ref.watch(billingBalanceProvider);
    final name = shop ?? user?.username ?? '';
    final hour = DateTime.now().hour;
    final greeting = hour < 12
        ? 'morning'
        : hour < 18
        ? 'afternoon'
        : 'evening';
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                i18n.t('light:home.dayGreeting.$greeting'),
                style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
              ),
              const SizedBox(height: 4),
              Text(
                name.isEmpty
                    ? i18n.t('light:home.defaultShop')
                    : i18n.t('light:home.greeting', vars: {'name': name}),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: FontSizes.xl3,
                  fontWeight: FontWeight.w800,
                  height: 1.2,
                  color: t.ink,
                ),
              ),
            ],
          ),
        ),
        const SizedBox(width: 12),
        Semantics(
          button: true,
          label: i18n.t('workspace:viewCredits'),
          child: Material(
            color: t.hairSoft,
            borderRadius: BorderRadius.circular(Radii.full),
            child: InkWell(
              key: const Key('light-credits'),
              borderRadius: BorderRadius.circular(Radii.full),
              onTap: () => context.push(Paths.billing('usage')),
              child: Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: 12,
                  vertical: 8,
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.toll_outlined, size: 15, color: t.a700),
                    const SizedBox(width: 6),
                    Text(
                      credits.hasError
                          ? i18n.t('light:home.creditsUnavailable')
                          : i18n.t(
                              'light:home.credits',
                              vars: {
                                'value': formatCreditsBrief(
                                  credits.valueOrNull?.balance,
                                ),
                              },
                            ),
                      style: TextStyle(
                        fontSize: FontSizes.sm,
                        fontWeight: FontWeight.w600,
                        color: t.ink,
                      ),
                    ),
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

class LightHomeHeading extends StatelessWidget {
  const LightHomeHeading(this.label, {super.key});

  final String label;

  @override
  Widget build(BuildContext context) => Semantics(
    header: true,
    child: Text(
      label,
      style: TextStyle(
        fontSize: FontSizes.xl2,
        fontWeight: FontWeight.w800,
        color: context.tokens.ink,
      ),
    ),
  );
}
