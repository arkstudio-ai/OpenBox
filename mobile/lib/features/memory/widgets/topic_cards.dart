import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/format.dart';
import '../models/wiki_models.dart';
import '../utils/knowledge_text.dart';
import 'knowledge_parts.dart';

/// Topic pages as cards (web `TopicGrid`): a title, a few lines to recognise
/// it by, and how well supported it is. Pages being rebuilt stay in place,
/// quietly marked. As a [rail] (the overview's preview) they swipe sideways
/// instead of stacking up the page.
class TopicCards extends StatelessWidget {
  const TopicCards({
    super.key,
    required this.topics,
    required this.query,
    required this.onOpen,
    this.rail = false,
  });

  final List<WikiSummary> topics;
  final String query;
  final ValueChanged<WikiSummary> onOpen;
  final bool rail;

  @override
  Widget build(BuildContext context) {
    if (!rail) {
      return Column(
        children: [
          for (final topic in topics)
            Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: _TopicCard(
                topic: topic,
                query: query,
                onTap: () => onOpen(topic),
              ),
            ),
        ],
      );
    }
    return LayoutBuilder(
      builder: (context, constraints) {
        final width = (constraints.maxWidth * 0.8).clamp(220.0, 340.0);
        // A handful of cards at most: one row as tall as its tallest card,
        // so a long title or a large type size never cuts a card short.
        return SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          // The rail runs to the screen edges while its cards line up with
          // the rest of the page.
          clipBehavior: Clip.none,
          child: IntrinsicHeight(
            child: ConstrainedBox(
              constraints: const BoxConstraints(minHeight: 168),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  for (var i = 0; i < topics.length; i++) ...[
                    if (i > 0) const SizedBox(width: 10),
                    SizedBox(
                      width: width,
                      child: _TopicCard(
                        topic: topics[i],
                        query: query,
                        fill: true,
                        onTap: () => onOpen(topics[i]),
                      ),
                    ),
                  ],
                ],
              ),
            ),
          ),
        );
      },
    );
  }
}

class _TopicCard extends ConsumerWidget {
  const _TopicCard({
    required this.topic,
    required this.query,
    required this.onTap,
    this.fill = false,
  });

  final WikiSummary topic;
  final String query;
  final VoidCallback onTap;

  /// Stretch to the rail's height, pinning the footer to the bottom.
  final bool fill;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final ready = topic.bodyAvailable;
    final updated = topic.updatedAt;
    final footer = TextStyle(fontSize: FontSizes.xs, color: t.n500);
    return Material(
      key: ValueKey('topic-card-${topic.id}'),
      color: ready ? t.card : t.hairSoft.withValues(alpha: 0.5),
      shape: RoundedRectangleBorder(
        side: BorderSide(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.xl),
      ),
      child: InkWell(
        customBorder: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(Radii.xl),
        ),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: fill ? MainAxisSize.max : MainAxisSize.min,
            children: [
              IconTile(
                icon: Icons.menu_book_outlined,
                size: 32,
                background: ready ? t.a100 : t.hairSoft,
                foreground: ready ? t.a700 : t.n500,
              ),
              const SizedBox(height: 10),
              HighlightText(
                topic.title,
                query: query,
                maxLines: 2,
                style: TextStyle(
                  fontSize: FontSizes.lg,
                  height: 1.35,
                  fontWeight: FontWeight.w500,
                  color: ready ? t.ink : t.n700,
                ),
              ),
              const SizedBox(height: 6),
              Text(
                ready
                    ? plainExcerpt(topic.excerpt, topic.title)
                    : i18n.t('knowledge:topic.updatingHint'),
                maxLines: 3,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  height: 1.6,
                  color: t.n600,
                ),
              ),
              if (fill) const Spacer() else const SizedBox(height: 12),
              if (ready)
                Text(
                  [
                    i18n.t('knowledge:topic.sources', count: topic.sourceCount),
                    if (updated != null) formatSince(updated, i18n.language),
                  ].join(' · '),
                  style: footer,
                )
              else
                Row(
                  children: [
                    Icon(Icons.sync, size: 12, color: t.n500),
                    const SizedBox(width: 5),
                    Text(i18n.t('knowledge:topic.updating'), style: footer),
                  ],
                ),
            ],
          ),
        ),
      ),
    );
  }
}
