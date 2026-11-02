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
import 'state/light_providers.dart';
import 'state/light_start.dart';
import 'widgets/entry_card.dart';
import 'widgets/home_header.dart';
import 'widgets/recent_groups.dart';

/// The Light home (docs `LIGHT_MODE_PLAN.md` §4.2, impl §4.2 T4): a heading,
/// the entries as cards the server ordered and gated, the 托管 band, 「最近做
/// 的」, and a fixed bar at the bottom for anything else — which is the
/// personal assistant.
class LightHomeScreen extends ConsumerWidget {
  const LightHomeScreen({super.key});

  Future<void> _refresh(WidgetRef ref) async {
    ref.invalidate(lightEntriesProvider);
    ref.invalidate(lightRecentProvider);
    ref.invalidate(lightShopNameProvider);
    ref.invalidate(billingBalanceProvider);
    try {
      await ref.read(lightEntriesProvider.future);
    } catch (_) {
      // The list shows its own error state.
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
      return;
    }
    if (!entry.available) {
      final target = lightUnavailableTarget(
        entry.availability,
        paidPlan: view.paidPlan,
      );
      if (target != null) context.push(target);
      return;
    }
    context.push(Paths.lightTopic(entry.id));
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
        child: Column(
          children: [
            Expanded(
              child: RefreshIndicator(
                onRefresh: () => _refresh(ref),
                child: ListView(
                  key: const Key('light-home'),
                  physics: const AlwaysScrollableScrollPhysics(),
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
                  children: [
                    const LightHomeHeader(),
                    const SizedBox(height: 20),
                    ...entries.when(
                      loading: () => const [_Skeleton()],
                      error: (error, _) => [
                        _Failed(
                          text: errorText(i18n, error),
                          retryLabel: i18n.t('common:action.retry'),
                          onRetry: () => ref.invalidate(lightEntriesProvider),
                        ),
                      ],
                      data: (view) => _cards(context, ref, i18n, view),
                    ),
                    const SizedBox(height: 20),
                    LightBanner(
                      title: i18n.t('light:home.banner.title'),
                      body: i18n.t('light:home.banner.body'),
                      onTap: () => context.push(Paths.lightAutopilot),
                    ),
                    const SizedBox(height: 24),
                    const LightRecentSection(),
                  ],
                ),
              ),
            ),
            _AskBar(
              label: i18n.t('light:home.ask'),
              onTap: () => context.go(Paths.lightAssistant),
            ),
          ],
        ),
      ),
    );
  }

  /// Hero for the first primary entry, half cards for the other primaries,
  /// a row of small cards for the secondary ones, grey for 即将上线.
  List<Widget> _cards(
    BuildContext context,
    WidgetRef ref,
    I18nState i18n,
    LightEntriesView view,
  ) {
    final t = context.tokens;
    final primaries = view.entries.where((e) => e.isPrimary).toList();
    final secondaries = view.entries.where((e) => e.isSecondary).toList();
    final coming = view.entries
        .where((e) => !e.isPrimary && !e.isSecondary)
        .toList();

    String? reason(LightEntry entry) => entry.available
        ? null
        : i18n.t('light:home.${lightAvailabilityKey(entry.availability)}');
    String? action(LightEntry entry) {
      if (entry.available || entry.isComingSoon) return null;
      final target = lightUnavailableTarget(
        entry.availability,
        paidPlan: view.paidPlan,
      );
      if (target == null) return null;
      if (target == Paths.desktop) return i18n.t('light:home.goDesktop');
      if (target.startsWith('/app/auth-center')) {
        return i18n.t('light:home.goBinding');
      }
      return i18n.t('light:home.goPlan');
    }

    Widget card(LightEntry entry, LightCardSize size) => LightEntryCard(
      key: Key('light-entry-${entry.id}'),
      entry: entry,
      size: size,
      reason: reason(entry),
      action: action(entry),
      onTap: () => _open(context, ref, view, entry),
    );
    Widget label(String key) => Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Text(
        i18n.t(key),
        style: TextStyle(
          fontSize: FontSizes.sm,
          fontWeight: FontWeight.w600,
          color: t.n700,
        ),
      ),
    );

    return [
      if (primaries.isNotEmpty) label('light:home.section.todo'),
      if (primaries.isNotEmpty) card(primaries.first, LightCardSize.hero),
      for (var i = 1; i < primaries.length; i += 2) ...[
        const SizedBox(height: 10),
        // Siblings share the taller one's height; a bare stretch inside the
        // list would ask for an infinite one.
        IntrinsicHeight(
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Expanded(child: card(primaries[i], LightCardSize.half)),
              const SizedBox(width: 10),
              Expanded(
                child: i + 1 < primaries.length
                    ? card(primaries[i + 1], LightCardSize.half)
                    : const SizedBox.shrink(),
              ),
            ],
          ),
        ),
      ],
      if (secondaries.isNotEmpty) ...[
        const SizedBox(height: 20),
        label('light:home.section.more'),
        IntrinsicHeight(
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              for (final (index, entry) in secondaries.take(3).indexed) ...[
                if (index > 0) const SizedBox(width: 8),
                Expanded(child: card(entry, LightCardSize.small)),
              ],
            ],
          ),
        ),
      ],
      for (final entry in coming) ...[
        const SizedBox(height: 10),
        card(entry, LightCardSize.half),
      ],
    ];
  }
}

/// 「有别的事，直接说」 — looks like an input, opens the 对话 tab.
class _AskBar extends StatelessWidget {
  const _AskBar({required this.label, required this.onTap});

  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: t.card,
      child: InkWell(
        key: const Key('light-ask'),
        onTap: onTap,
        child: Container(
          decoration: BoxDecoration(
            border: Border(top: BorderSide(color: t.hair)),
          ),
          padding: const EdgeInsets.fromLTRB(16, 10, 12, 10),
          child: Row(
            children: [
              Icon(Icons.mic_none_outlined, size: 20, color: t.n600),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  label,
                  style: TextStyle(fontSize: FontSizes.base, color: t.n500),
                ),
              ),
              Container(
                width: 32,
                height: 32,
                decoration: BoxDecoration(color: t.ink, shape: BoxShape.circle),
                child: Icon(Icons.arrow_upward, size: 18, color: t.bg),
              ),
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
    final t = context.tokens;
    Widget box(double height) => Container(
      height: height,
      decoration: BoxDecoration(
        color: t.hairSoft,
        borderRadius: BorderRadius.circular(Radii.xl),
      ),
    );
    return Column(
      children: [
        box(132),
        const SizedBox(height: 10),
        Row(
          children: [
            Expanded(child: box(112)),
            const SizedBox(width: 10),
            Expanded(child: box(112)),
          ],
        ),
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
