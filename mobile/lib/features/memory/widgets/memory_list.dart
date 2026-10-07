import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/format.dart';
import '../models/memory_models.dart';
import 'knowledge_parts.dart';

const _maxTopics = 2;

/// Remembered facts as one quiet list (web `MemoryList`): the sentence first,
/// where it lives and when it changed second, the two things people do with
/// it at the end.
class MemoryList extends StatelessWidget {
  const MemoryList({
    super.key,
    required this.memories,
    required this.query,
    required this.topicsOf,
    required this.scopeName,
    required this.onOpen,
    required this.onEdit,
    required this.onForget,
    required this.onTopic,
  });

  final List<MemoryRecord> memories;
  final String query;
  final Map<String, List<MemoryTopic>> topicsOf;
  final String Function(MemoryRecord memory) scopeName;
  final ValueChanged<MemoryRecord> onOpen;
  final ValueChanged<MemoryRecord> onEdit;
  final ValueChanged<MemoryRecord> onForget;
  final ValueChanged<MemoryTopic> onTopic;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return KnowledgeCard(
      child: Column(
        children: [
          for (var i = 0; i < memories.length; i++) ...[
            if (i > 0) Divider(height: 1, thickness: 1, color: t.hair),
            _MemoryRow(
              key: ValueKey('memory-row-${memories[i].id}'),
              memory: memories[i],
              list: this,
            ),
          ],
        ],
      ),
    );
  }
}

class _MemoryRow extends ConsumerWidget {
  const _MemoryRow({super.key, required this.memory, required this.list});

  final MemoryRecord memory;
  final MemoryList list;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final scope = list.scopeName(memory);
    final topics = [
      for (final topic in list.topicsOf[memory.id] ?? const <MemoryTopic>[])
        if (topic.pageId != null) topic,
    ];
    final meta = TextStyle(fontSize: FontSizes.xs, color: t.n600);
    final updated = memory.updatedAt;
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          // The sentence itself is the way in.
          child: Semantics(
            button: true,
            hint: i18n.t('knowledge:memory.openDetail'),
            child: InkWell(
              onTap: () => list.onOpen(memory),
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 13, 4, 13),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    HighlightText(
                      memory.summary,
                      query: list.query,
                      style: TextStyle(
                        fontSize: FontSizes.base,
                        height: 1.6,
                        color: t.ink,
                      ),
                    ),
                    const SizedBox(height: 6),
                    Wrap(
                      spacing: 8,
                      runSpacing: 6,
                      crossAxisAlignment: WrapCrossAlignment.center,
                      children: [
                        if (scope.isNotEmpty) Text(scope, style: meta),
                        if (scope.isNotEmpty && updated != null)
                          ExcludeSemantics(child: Text('·', style: meta)),
                        if (updated != null)
                          Text(
                            formatSince(updated, i18n.language),
                            style: meta,
                          ),
                        for (final topic in topics.take(_maxTopics))
                          _TopicChip(
                            title: topic.title,
                            onTap: () => list.onTopic(topic),
                          ),
                        if (topics.length > _maxTopics)
                          Text(
                            i18n.t(
                              'knowledge:memory.moreTopics',
                              vars: {'count': topics.length - _maxTopics},
                            ),
                            style: TextStyle(
                              fontSize: FontSizes.xs,
                              color: t.n500,
                            ),
                          ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
        Padding(
          padding: const EdgeInsets.only(top: 6, right: 4),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              IconButton(
                key: ValueKey('memory-edit-${memory.id}'),
                tooltip: i18n.t('knowledge:memory.edit'),
                visualDensity: VisualDensity.compact,
                icon: Icon(Icons.edit_outlined, size: 18, color: t.n600),
                onPressed: () => list.onEdit(memory),
              ),
              IconButton(
                key: ValueKey('memory-forget-${memory.id}'),
                tooltip: i18n.t('knowledge:memory.forget'),
                visualDensity: VisualDensity.compact,
                icon: Icon(Icons.delete_outline, size: 18, color: t.n600),
                onPressed: () => list.onForget(memory),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

/// A memory's link to a topic it belongs to.
class _TopicChip extends StatelessWidget {
  const _TopicChip({required this.title, required this.onTap});

  final String title;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Semantics(
      link: true,
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.full),
        onTap: onTap,
        child: Container(
          constraints: const BoxConstraints(maxWidth: 180),
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
          decoration: BoxDecoration(
            color: t.hairSoft,
            borderRadius: BorderRadius.circular(Radii.full),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.menu_book_outlined, size: 11, color: t.n700),
              const SizedBox(width: 4),
              Flexible(
                child: Text(
                  title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
