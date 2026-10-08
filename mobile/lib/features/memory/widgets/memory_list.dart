import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/format.dart';
import '../models/memory_models.dart';
import '../utils/knowledge_text.dart' show formatDay;
import 'knowledge_parts.dart';
import 'knowledge_sheets.dart';

enum _MemoryAction { edit, forget }

/// Remembered facts as one quiet list (web `MemoryList`): the sentence first,
/// where it lives and when it changed under it. A tap opens the memory; a
/// long press offers editing or forgetting it.
class MemoryList extends ConsumerWidget {
  const MemoryList({
    super.key,
    required this.memories,
    required this.query,
    required this.scopeName,
    required this.onOpen,
    required this.onEdit,
    required this.onForget,
    this.footer,
    this.timeline = false,
  });

  final List<MemoryRecord> memories;

  /// Under headings by when each changed (today, yesterday, the past week,
  /// earlier): what was learned lately reads first. The list is already
  /// newest first.
  final bool timeline;
  final String query;
  final String Function(MemoryRecord memory) scopeName;
  final ValueChanged<MemoryRecord> onOpen;
  final ValueChanged<MemoryRecord> onEdit;
  final ValueChanged<MemoryRecord> onForget;

  /// A last row, such as "load more".
  final Widget? footer;

  Future<void> _actions(
    BuildContext context,
    I18nState i18n,
    MemoryRecord memory,
  ) async {
    unawaited(HapticFeedback.selectionClick());
    final action = await showActionSheet<_MemoryAction>(
      context,
      title: memory.summary,
      actions: [
        SheetAction(
          key: const ValueKey('memory-action-edit'),
          value: _MemoryAction.edit,
          label: i18n.t('knowledge:memory.edit'),
          icon: Icons.edit_outlined,
        ),
        SheetAction(
          key: const ValueKey('memory-action-forget'),
          value: _MemoryAction.forget,
          label: i18n.t('knowledge:memory.forget'),
          icon: Icons.delete_outline,
          danger: true,
        ),
      ],
    );
    switch (action) {
      case _MemoryAction.edit:
        onEdit(memory);
      case _MemoryAction.forget:
        onForget(memory);
      case null:
        break;
    }
  }

  Widget _row(BuildContext context, MemoryRecord memory) => _MemoryRow(
    key: ValueKey('memory-row-${memory.id}'),
    memory: memory,
    query: query,
    scope: scopeName(memory),
    onOpen: () => onOpen(memory),
    onActions: (i18n) => _actions(context, i18n, memory),
    onEdit: () => onEdit(memory),
    onForget: () => onForget(memory),
  );

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (!timeline) {
      return KnowledgeGroup(
        children: [
          for (final memory in memories) _row(context, memory),
          ?footer,
        ],
      );
    }
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final periods = <(String, List<MemoryRecord>)>[];
    for (final memory in memories) {
      final period = memoryPeriod(memory.updatedAt);
      if (periods.isNotEmpty && periods.last.$1 == period) {
        periods.last.$2.add(memory);
      } else {
        periods.add((period, [memory]));
      }
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final (index, (period, items)) in periods.indexed) ...[
          Padding(
            padding: EdgeInsets.only(
              left: 4,
              bottom: 6,
              top: index > 0 ? 14 : 0,
            ),
            child: Text(
              i18n.t('knowledge:timeline.$period'),
              key: ValueKey('memory-period-$period'),
              style: TextStyle(
                fontSize: FontSizes.xs,
                fontWeight: FontWeight.w500,
                color: t.n600,
              ),
            ),
          ),
          KnowledgeGroup(
            children: [
              for (final memory in items) _row(context, memory),
              if (index == periods.length - 1) ?footer,
            ],
          ),
        ],
      ],
    );
  }
}

/// today / yesterday / week (the six days before) / earlier, by local day.
String memoryPeriod(DateTime? at, {DateTime? now}) {
  if (at == null) return 'earlier';
  final current = (now ?? DateTime.now()).toLocal();
  final today = DateTime(current.year, current.month, current.day);
  final local = at.toLocal();
  if (!local.isBefore(today)) return 'today';
  if (!local.isBefore(today.subtract(const Duration(days: 1)))) {
    return 'yesterday';
  }
  if (!local.isBefore(today.subtract(const Duration(days: 6)))) return 'week';
  return 'earlier';
}

/// How the person came to have it: learned from a chat or confirmed by them;
/// how they like to be helped; and, for a plan, its last day.
List<String> howLearned(MemoryRecord memory, I18nState i18n) => [
  if (memory.owner == 'SYSTEM_VERIFIED') i18n.t('knowledge:how.learned'),
  if (memory.owner == 'USER_CONFIRMED') i18n.t('knowledge:how.added'),
  if (memory.factKey?.startsWith('personal.style.') ?? false)
    i18n.t('knowledge:how.style'),
  // A plan lasts to the end of its last day: the moment it expires is the
  // next midnight.
  if (memory.expiresAt case final end?)
    i18n.t(
      'knowledge:how.until',
      vars: {
        'date': formatDay(
          end.subtract(const Duration(seconds: 1)),
          i18n.language,
        ),
      },
    ),
];

class _MemoryRow extends ConsumerWidget {
  const _MemoryRow({
    super.key,
    required this.memory,
    required this.query,
    required this.scope,
    required this.onOpen,
    required this.onActions,
    required this.onEdit,
    required this.onForget,
  });

  final MemoryRecord memory;
  final String query;
  final String scope;
  final VoidCallback onOpen;
  final ValueChanged<I18nState> onActions;
  final VoidCallback onEdit;
  final VoidCallback onForget;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final updated = memory.updatedAt;
    final meta = [
      if (scope.isNotEmpty) scope,
      if (updated != null) formatSince(updated, i18n.language),
      ...howLearned(memory, i18n),
    ].join(' · ');
    return Semantics(
      button: true,
      hint: i18n.t('knowledge:memory.openDetail'),
      // Editing and forgetting stay reachable without a long press.
      customSemanticsActions: {
        CustomSemanticsAction(label: i18n.t('knowledge:memory.edit')): onEdit,
        CustomSemanticsAction(label: i18n.t('knowledge:memory.forget')):
            onForget,
      },
      child: InkWell(
        onTap: onOpen,
        onLongPress: () => onActions(i18n),
        child: Padding(
          padding: knowledgeRowPadding,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              HighlightText(
                memory.summary,
                query: query,
                maxLines: 2,
                style: TextStyle(
                  fontSize: FontSizes.base,
                  height: 1.5,
                  color: t.ink,
                ),
              ),
              if (meta.isNotEmpty) ...[
                const SizedBox(height: 3),
                Text(
                  meta,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
