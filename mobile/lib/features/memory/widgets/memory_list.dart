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
import 'knowledge_parts.dart';
import 'knowledge_sheets.dart';

enum _MemoryAction { edit, forget }

/// Remembered facts as one quiet list (web `MemoryList`): the sentence first,
/// where it lives and when it changed under it. A tap opens the memory; a
/// long press offers editing or forgetting it.
class MemoryList extends StatelessWidget {
  const MemoryList({
    super.key,
    required this.memories,
    required this.query,
    required this.scopeName,
    required this.onOpen,
    required this.onEdit,
    required this.onForget,
    this.footer,
  });

  final List<MemoryRecord> memories;
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

  @override
  Widget build(BuildContext context) => KnowledgeGroup(
    children: [
      for (final memory in memories)
        _MemoryRow(
          key: ValueKey('memory-row-${memory.id}'),
          memory: memory,
          query: query,
          scope: scopeName(memory),
          onOpen: () => onOpen(memory),
          onActions: (i18n) => _actions(context, i18n, memory),
          onEdit: () => onEdit(memory),
          onForget: () => onForget(memory),
        ),
      ?footer,
    ],
  );
}

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
