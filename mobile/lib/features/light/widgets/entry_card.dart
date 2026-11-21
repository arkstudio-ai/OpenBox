import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/models/light.dart';

IconData lightEntryIcon(String id) => switch (id) {
  'video' => Icons.movie_outlined,
  'voice_assistant' => Icons.phone_outlined,
  'menu' => Icons.article_outlined,
  'poster' => Icons.add_photo_alternate_outlined,
  'douyin_publish' => Icons.send_outlined,
  'review' => Icons.insights_outlined,
  'shopkeeper' => Icons.storefront_outlined,
  'footage_edit' => Icons.content_cut,
  _ => Icons.auto_awesome_outlined,
};

/// A production entry keeps its access action even when it is gated.
class LightEntryCard extends StatelessWidget {
  const LightEntryCard({
    super.key,
    required this.entry,
    required this.subtitle,
    required this.onTap,
    this.featured = false,
    this.reason,
  });

  final LightEntry entry;
  final String subtitle;
  final VoidCallback onTap;
  final bool featured;
  final String? reason;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final unavailable = !entry.available;
    final foreground = unavailable ? t.n700 : t.ink;
    final radius = BorderRadius.circular(Radii.xl);
    final icon = Icon(lightEntryIcon(entry.id), color: foreground, size: 26);
    final label = Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          entry.title,
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w800,
            height: 1.2,
            color: foreground,
          ),
        ),
        const SizedBox(height: 4),
        Text(
          reason ?? subtitle,
          style: TextStyle(fontSize: FontSizes.sm, height: 1.3, color: t.n700),
        ),
      ],
    );
    return Material(
      color: featured || unavailable ? t.hairSoft : t.card,
      shape: RoundedRectangleBorder(
        borderRadius: radius,
        side: BorderSide(color: t.hair),
      ),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: 100),
          child: Padding(
            padding: const EdgeInsets.all(14),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [icon, const SizedBox(height: 7), label],
            ),
          ),
        ),
      ),
    );
  }
}
