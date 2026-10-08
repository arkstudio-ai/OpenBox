import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/format.dart';
import '../models/wiki_models.dart';
import '../utils/knowledge_text.dart';
import 'knowledge_parts.dart';

/// Topic pages as a list to read from (web `TopicGrid`), the way a notes app
/// lists notes: the title, then when it changed and how it begins. Pages
/// being rebuilt stay in place, quietly marked.
class TopicList extends StatelessWidget {
  const TopicList({
    super.key,
    required this.topics,
    required this.query,
    required this.onOpen,
    this.footer,
  });

  final List<WikiSummary> topics;
  final String query;
  final ValueChanged<WikiSummary> onOpen;

  /// A last row, such as "load more".
  final Widget? footer;

  @override
  Widget build(BuildContext context) => KnowledgeGroup(
    children: [
      for (final topic in topics)
        _TopicRow(
          key: ValueKey('topic-row-${topic.id}'),
          topic: topic,
          query: query,
          onTap: () => onOpen(topic),
        ),
      ?footer,
    ],
  );
}

class _TopicRow extends ConsumerWidget {
  const _TopicRow({
    super.key,
    required this.topic,
    required this.query,
    required this.onTap,
  });

  final WikiSummary topic;
  final String query;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final ready = topic.bodyAvailable;
    final updated = topic.updatedAt;
    final excerpt = ready
        ? plainExcerpt(topic.excerpt, topic.title)
        : i18n.t('knowledge:topic.updatingHint');
    final lead = !ready || updated != null;
    return Semantics(
      button: true,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: knowledgeRowPadding,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              HighlightText(
                topic.title,
                query: query,
                maxLines: 1,
                style: TextStyle(
                  fontSize: FontSizes.base,
                  height: 1.45,
                  fontWeight: FontWeight.w500,
                  color: ready ? t.ink : t.n700,
                ),
              ),
              const SizedBox(height: 3),
              Text.rich(
                TextSpan(
                  children: [
                    if (!ready) ...[
                      WidgetSpan(
                        alignment: PlaceholderAlignment.middle,
                        child: Padding(
                          padding: const EdgeInsets.only(right: 4),
                          child: Icon(Icons.sync, size: 12, color: t.n600),
                        ),
                      ),
                      TextSpan(
                        text: i18n.t('knowledge:topic.updating'),
                        style: TextStyle(color: t.n700),
                      ),
                    ] else if (updated != null)
                      TextSpan(
                        text: formatSince(updated, i18n.language),
                        style: TextStyle(color: t.n700),
                      ),
                    if (excerpt.isNotEmpty)
                      TextSpan(text: lead ? '  $excerpt' : excerpt),
                  ],
                ),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  height: 1.45,
                  color: t.n600,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
