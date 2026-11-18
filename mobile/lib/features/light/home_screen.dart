import 'dart:async';

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
import '../chat/state/config_providers.dart';
import '../store/models/store.dart';
import '../store/state/store_provider.dart';
import '../voice/widgets/voice_call_button.dart';
import 'production/production_api.dart';
import 'state/light_providers.dart';
import 'state/light_start.dart';
import 'widgets/entry_card.dart';
import 'widgets/home_header.dart';
import 'widgets/recent_groups.dart';
import 'widgets/starter_card_tile.dart';

/// The Light home (docs `LIGHT_MODE_PLAN.md` §4.2, impl §4.2 T4): a heading,
/// the entries as cards the server ordered and gated, the 托管 band, 「最近做
/// 的」, and a fixed bar at the bottom for anything else — which is the
/// personal assistant.
class LightHomeScreen extends ConsumerWidget {
  const LightHomeScreen({super.key});

  Future<void> _refresh(WidgetRef ref) async {
    ref.invalidate(lightEntriesProvider);
    ref.invalidate(lightRecentProvider);
    ref.invalidate(storeProvider);
    ref.invalidate(billingBalanceProvider);
    try {
      await ref.read(lightEntriesProvider.future);
    } catch (_) {
      // The list shows its own error state.
    }
  }

  /// The voice card can dial only once the assistant scope is bound and the
  /// deployment has voice calls; until then it is grey and leads to the
  /// assistant tab, where the same call button lives.
  bool _voiceReady(WidgetRef ref) =>
      ref.watch(assistantScopeProvider) != null &&
      (ref.watch(appConfigProvider).valueOrNull?.voiceEnabled ?? false);

  void _open(
    BuildContext context,
    WidgetRef ref,
    LightEntriesView view,
    LightEntry entry, {
    String? brief,
  }) {
    if (entry.isVoiceCall) {
      if (_voiceReady(ref)) {
        unawaited(openVoiceCall(context, ref));
      } else {
        context.go(Paths.lightAssistant);
      }
      return;
    }
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
    context.push(
      ProductionKind.fromId(entry.id) == null
          ? Paths.lightTopic(entry.id)
          : Paths.lightProductionStart(entry.id, brief: brief),
    );
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
    final voiceReady = _voiceReady(ref);
    // A card only promises what this workspace can do right now.
    final starters = [
      for (final card
          in ref.watch(storeStarterCardsProvider).valueOrNull ??
              const <StarterCard>[])
        if (view.byId(card.entryId)?.available ?? false) card,
    ];
    final primaries = [
      for (final kind in ProductionKind.values) ?view.byId(kind.id),
    ];
    final secondaries = view.entries
        .where(
          (e) =>
              ProductionKind.fromId(e.id) == null &&
              (e.isPrimary || e.isSecondary),
        )
        .toList();
    final coming = view.entries
        .where(
          (e) =>
              ProductionKind.fromId(e.id) == null &&
              !e.isPrimary &&
              !e.isSecondary,
        )
        .toList();

    bool voiceOff(LightEntry entry) => entry.isVoiceCall && !voiceReady;
    String? reason(LightEntry entry) => voiceOff(entry)
        ? i18n.t('light:home.voiceUnavailable')
        : entry.available
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
      unavailable: voiceOff(entry),
      subtitle: ProductionKind.fromId(entry.id) == null
          ? null
          : i18n.t('light:production.home.${entry.id}'),
      showPriceHint: ProductionKind.fromId(entry.id) == null,
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
      const SizedBox(height: 20),
      label('light:home.section.todo'),
      for (final starter in starters) ...[
        LightStarterCardTile(
          key: Key('light-starter-${starter.entryId}'),
          card: starter,
          onTap: () => _open(
            context,
            ref,
            view,
            view.byId(starter.entryId)!,
            brief: starter.title,
          ),
        ),
        const SizedBox(height: 8),
      ],
      if (starters.isEmpty)
        Padding(
          padding: const EdgeInsets.only(bottom: 10),
          child: Text(
            i18n.t('light:production.noSuggestions'),
            style: TextStyle(color: t.n700),
          ),
        ),
      Material(
        color: t.bg,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(16),
          side: BorderSide(color: t.hair),
        ),
        child: ListTile(
          key: const Key('light-production-history'),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
          ),
          leading: Icon(Icons.history, color: t.ink),
          title: Text(
            i18n.t('light:production.history'),
            style: TextStyle(color: t.ink, fontWeight: FontWeight.w700),
          ),
          trailing: Icon(Icons.chevron_right, color: t.n700),
          onTap: () => context.push(Paths.lightProductionHistory),
        ),
      ),
      if (secondaries.isNotEmpty) ...[
        const SizedBox(height: 20),
        label('light:home.section.more'),
        for (var i = 0; i < secondaries.length; i += 2)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: IntrinsicHeight(
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Expanded(child: card(secondaries[i], LightCardSize.small)),
                  const SizedBox(width: 8),
                  if (i + 1 < secondaries.length)
                    Expanded(
                      child: card(secondaries[i + 1], LightCardSize.small),
                    )
                  else
                    const Spacer(),
                ],
              ),
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
/// Not a composer: a line of help under the cards that points at the
/// assistant tab, where anything else is said (2026-11-04 feedback — a
/// fake input here looked like a second place to type).
class _AskBar extends StatelessWidget {
  const _AskBar({required this.label, required this.onTap});

  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: t.bg,
      child: InkWell(
        key: const Key('light-ask'),
        onTap: onTap,
        child: Container(
          decoration: BoxDecoration(
            border: Border(top: BorderSide(color: t.hairSoft)),
          ),
          padding: const EdgeInsets.fromLTRB(16, 10, 12, 10),
          child: Row(
            children: [
              Icon(Icons.auto_awesome_outlined, size: 16, color: t.a700),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  label,
                  style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                ),
              ),
              Icon(Icons.chevron_right, size: 18, color: t.n500),
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
