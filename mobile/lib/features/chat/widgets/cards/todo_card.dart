import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/todo.dart';
import '../../../../shared/widgets/shimmer_text.dart';
import '../../../../shared/widgets/spinner.dart';

/// Task-list card (web `TodoCard`): checkmark timeline with the in-progress
/// item shimmering its active form.
class TodoCard extends ConsumerWidget {
  const TodoCard({super.key, required this.items});

  final List<TodoItem> items;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final done = items.where((i) => i.status == TodoStatus.completed).length;
    final allDone = items.isNotEmpty && done == items.length;
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 10),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.xl),
        border: Border.all(color: t.hair),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  i18n.t('chat:todo.title'),
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w600,
                    color: t.n800,
                  ),
                ),
              ),
              Text(
                allDone
                    ? i18n.t('chat:todo.allDone', vars: {'total': items.length})
                    : '$done / ${items.length}',
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
            ],
          ),
          const SizedBox(height: 10),
          for (final item in items) _TodoRow(item: item),
        ],
      ),
    );
  }
}

class _TodoRow extends StatelessWidget {
  const _TodoRow({required this.item});

  final TodoItem item;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final active = item.status == TodoStatus.inProgress;
    final done = item.status == TodoStatus.completed;
    final cancelled = item.status == TodoStatus.cancelled;
    final label = active && item.activeForm != null && item.activeForm!.isNotEmpty
        ? item.activeForm!
        : item.subject;
    final style = TextStyle(
      fontSize: FontSizes.md,
      height: 1.5,
      color: done || cancelled ? t.n500 : t.ink,
      decoration: cancelled ? TextDecoration.lineThrough : null,
    );
    return Padding(
      padding: const EdgeInsets.only(bottom: 7),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: 2),
            child: done
                ? Icon(Icons.check_circle, size: 16, color: t.s600)
                : active
                    ? const Spinner(size: 14)
                    : Icon(Icons.circle_outlined, size: 15, color: t.n400),
          ),
          const SizedBox(width: 9),
          Expanded(
            child: active
                ? ShimmerText(label, style: style)
                : Text(label, style: style),
          ),
        ],
      ),
    );
  }
}
