import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/light.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/widgets/toast.dart';
import '../billing/state/billing_providers.dart';
import '../chat/api/assistant_api.dart';
import '../chat/state/assistant_overview.dart';
import '../store/state/store_provider.dart';
import 'production/production_api.dart';
import 'state/light_providers.dart';
import 'state/light_start.dart';
import 'widgets/entry_card.dart';
import 'widgets/home_assistant.dart';
import 'widgets/home_header.dart';

class LightHomeScreen extends ConsumerWidget {
  const LightHomeScreen({super.key});

  Future<void> _refresh(WidgetRef ref) async {
    ref.invalidate(lightEntriesProvider);
    ref.invalidate(storeProvider);
    ref.invalidate(billingBalanceProvider);
    final scope = ref.read(assistantScopeProvider);
    if (scope != null) ref.invalidate(assistantOverviewProvider(scope));
    try {
      await ref.read(lightEntriesProvider.future);
    } catch (_) {
      // The production section shows its own error state.
    }
  }

  void _open(
    BuildContext context,
    WidgetRef ref,
    LightEntriesView view,
    LightEntry entry,
  ) {
    if (entry.isComingSoon) {
      ref
          .read(toastProvider.notifier)
          .info(ref.read(i18nProvider).t('light:home.comingSoon'));
    } else if (!entry.available) {
      final target = lightUnavailableTarget(
        entry.availability,
        paidPlan: view.paidPlan,
      );
      if (target != null) context.push(target);
    } else {
      context.push(Paths.lightProductionStart(entry.id));
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final entries = ref.watch(lightEntriesProvider);
    return Scaffold(
      backgroundColor: t.bg,
      body: SafeArea(
        bottom: false,
        child: RefreshIndicator(
          onRefresh: () => _refresh(ref),
          child: ListView(
            key: const Key('light-home'),
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
            children: [
              const LightHomeHeader(),
              const SizedBox(height: 18),
              Row(
                children: [
                  Expanded(
                    child: LightHomeHeading(i18n.t('light:home.production')),
                  ),
                  TextButton.icon(
                    key: const Key('light-production-history'),
                    onPressed: () => context.push(Paths.lightProductionHistory),
                    icon: const Icon(Icons.history, size: 19),
                    label: Text(i18n.t('light:production.history')),
                    style: TextButton.styleFrom(
                      foregroundColor: t.n800,
                      textStyle: const TextStyle(
                        fontSize: FontSizes.sm,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 5),
              entries.when(
                loading: () => const _Skeleton(),
                error: (error, _) => _Failed(
                  text: errorText(i18n, error),
                  retryLabel: i18n.t('common:action.retry'),
                  onRetry: () => ref.invalidate(lightEntriesProvider),
                ),
                data: (view) {
                  final entries = [
                    for (final kind in ProductionKind.values)
                      ?view.byId(kind.id),
                  ];
                  Widget card(LightEntry entry) => LightEntryCard(
                    key: Key('light-entry-${entry.id}'),
                    entry: entry,
                    featured: entry.id == ProductionKind.video.id,
                    subtitle: i18n.t('light:production.home.${entry.id}'),
                    reason: entry.available
                        ? null
                        : i18n.t(
                            'light:home.${lightAvailabilityKey(entry.availability)}',
                          ),
                    onTap: () => _open(context, ref, view, entry),
                  );
                  return Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      for (
                        var index = 0;
                        index < entries.length;
                        index += 2
                      ) ...[
                        if (index > 0) const SizedBox(height: 10),
                        Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Expanded(child: card(entries[index])),
                            const SizedBox(width: 10),
                            Expanded(
                              child: index + 1 < entries.length
                                  ? card(entries[index + 1])
                                  : const SizedBox.shrink(),
                            ),
                          ],
                        ),
                      ],
                    ],
                  );
                },
              ),
              const SizedBox(height: 22),
              const LightHomeAssistant(),
            ],
          ),
        ),
      ),
    );
  }
}

class _Skeleton extends StatelessWidget {
  const _Skeleton();

  @override
  Widget build(BuildContext context) {
    Widget box(double height) => Container(
      height: height,
      decoration: BoxDecoration(
        color: context.tokens.hairSoft,
        borderRadius: BorderRadius.circular(Radii.xl),
      ),
    );
    return Column(
      children: [
        for (var row = 0; row < 2; row++) ...[
          if (row > 0) const SizedBox(height: 10),
          Row(
            children: [
              Expanded(child: box(100)),
              const SizedBox(width: 10),
              Expanded(child: box(100)),
            ],
          ),
        ],
      ],
    );
  }
}

class _Failed extends StatelessWidget {
  const _Failed({
    required this.text,
    required this.retryLabel,
    required this.onRetry,
  });

  final String text;
  final String retryLabel;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      key: const Key('light-entries-failed'),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: t.hairSoft,
        borderRadius: BorderRadius.circular(Radii.xl),
      ),
      child: Column(
        children: [
          Text(
            text,
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
          ),
          TextButton(onPressed: onRetry, child: Text(retryLabel)),
        ],
      ),
    );
  }
}
