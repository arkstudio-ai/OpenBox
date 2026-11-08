import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/models/light.dart';

/// The glyph of a home entry, by its id (the server names no icon).
IconData lightEntryIcon(String id) => switch (id) {
  'video' => Icons.videocam_outlined,
  'voice_assistant' => Icons.phone_outlined,
  'menu' => Icons.restaurant_menu,
  'poster' => Icons.image_outlined,
  'douyin_publish' => Icons.send_outlined,
  'review' => Icons.insights_outlined,
  'shopkeeper' => Icons.storefront_outlined,
  'footage_edit' => Icons.movie_filter_outlined,
  _ => Icons.auto_awesome_outlined,
};

enum LightCardSize { hero, half, small }

/// One home entry (docs `LIGHT_MODE_PLAN.md` §4.2): a verb on a card. Three
/// sizes share one look; an entry the workspace cannot use yet is drawn
/// grey with the reason and still tappable — the tap goes where the block
/// is lifted, not nowhere.
class LightEntryCard extends StatelessWidget {
  const LightEntryCard({
    super.key,
    required this.entry,
    required this.size,
    required this.onTap,
    this.reason,
    this.action,
    this.unavailable = false,
  });

  final LightEntry entry;
  final LightCardSize size;
  final VoidCallback? onTap;

  /// Grey for a reason the server does not know (the voice card when this
  /// deployment has no voice calls); [reason] says which.
  final bool unavailable;

  /// Why the entry is grey (`needs_desktop` …), already translated.
  final String? reason;

  /// The grey card's call to action ("去开通"), already translated.
  final String? action;

  bool get _grey => unavailable || !entry.available;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final hero = size == LightCardSize.hero && !_grey;
    final bg = _grey
        ? t.hairSoft
        : hero
        ? t.a700
        : t.card;
    final ink = _grey
        ? t.n600
        : hero
        ? t.bg
        : t.ink;
    final sub = _grey
        ? t.n500
        : hero
        ? t.a100
        : t.n600;
    final radius = BorderRadius.circular(Radii.xl);
    return Semantics(
      button: true,
      label: entry.title,
      child: Material(
        color: bg,
        borderRadius: radius,
        child: InkWell(
          borderRadius: radius,
          onTap: onTap,
          child: Container(
            constraints: BoxConstraints(
              minHeight: switch (size) {
                LightCardSize.hero => 132,
                LightCardSize.half => 112,
                LightCardSize.small => 92,
              },
            ),
            padding: EdgeInsets.all(size == LightCardSize.small ? 12 : 16),
            decoration: BoxDecoration(
              borderRadius: radius,
              border: hero ? null : Border.all(color: t.hair),
            ),
            child: switch (size) {
              LightCardSize.small => _small(t, ink, sub),
              _ => _large(t, ink, sub, hero),
            },
          ),
        ),
      ),
    );
  }

  Widget _large(BossipTokens t, Color ink, Color sub, bool hero) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    mainAxisAlignment: MainAxisAlignment.spaceBetween,
    children: [
      Row(
        children: [
          Icon(
            lightEntryIcon(entry.id),
            size: size == LightCardSize.hero ? 26 : 22,
            color: ink,
          ),
          const Spacer(),
          if (_grey)
            Icon(Icons.lock_outline, size: 16, color: sub)
          else
            Icon(Icons.arrow_forward, size: 18, color: sub),
        ],
      ),
      SizedBox(height: size == LightCardSize.hero ? 18 : 12),
      Text(
        entry.title,
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
        style: TextStyle(
          fontSize: size == LightCardSize.hero ? FontSizes.xl3 : FontSizes.xl,
          fontWeight: FontWeight.w800,
          height: 1.15,
          color: ink,
        ),
      ),
      const SizedBox(height: 4),
      Text(
        _grey && reason != null ? reason! : entry.subtitle,
        maxLines: 2,
        overflow: TextOverflow.ellipsis,
        style: TextStyle(fontSize: FontSizes.sm, height: 1.35, color: sub),
      ),
      if (size == LightCardSize.hero && entry.priceHint.isNotEmpty) ...[
        const SizedBox(height: 8),
        Text(
          entry.priceHint,
          style: TextStyle(fontSize: FontSizes.xs, color: sub),
        ),
      ],
      if (_grey && action != null) ...[
        const SizedBox(height: 8),
        Text(
          action!,
          style: TextStyle(
            fontSize: FontSizes.sm,
            fontWeight: FontWeight.w600,
            color: t.a700,
          ),
        ),
      ],
    ],
  );

  Widget _small(BossipTokens t, Color ink, Color sub) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Icon(lightEntryIcon(entry.id), size: 20, color: ink),
      const SizedBox(height: 10),
      // Two lines: English and Vietnamese titles run past a third of the
      // width; the row is IntrinsicHeight, so the three cards stay level.
      Text(
        entry.title,
        maxLines: 2,
        overflow: TextOverflow.ellipsis,
        style: TextStyle(
          fontSize: FontSizes.base,
          fontWeight: FontWeight.w700,
          color: ink,
        ),
      ),
      if (_grey && reason != null) ...[
        const SizedBox(height: 2),
        Text(
          reason!,
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(fontSize: FontSizes.xs2, color: sub),
        ),
      ],
    ],
  );
}

/// 「所有事都能省心托管」 — the black band under the entries; opens 托管中的事.
class LightBanner extends StatelessWidget {
  const LightBanner({
    super.key,
    required this.title,
    required this.body,
    required this.onTap,
  });

  final String title;
  final String body;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final radius = BorderRadius.circular(Radii.xl);
    return Material(
      color: t.n900,
      borderRadius: radius,
      child: InkWell(
        borderRadius: radius,
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
          child: Row(
            children: [
              Icon(Icons.schedule_outlined, size: 22, color: t.n100),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      style: TextStyle(
                        fontSize: FontSizes.lg,
                        fontWeight: FontWeight.w700,
                        color: t.n100,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      body,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: FontSizes.sm,
                        height: 1.35,
                        color: t.n400,
                      ),
                    ),
                  ],
                ),
              ),
              Icon(Icons.chevron_right, size: 20, color: t.n400),
            ],
          ),
        ),
      ),
    );
  }
}
