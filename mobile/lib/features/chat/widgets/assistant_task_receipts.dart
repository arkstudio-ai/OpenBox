import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/models/message_part.dart';
import '../api/assistant_api.dart';
import '../utils/task_receipt.dart';
import 'assistant_task_card.dart';

/// Cards for the tasks a turn started or changed, one per task even when the
/// turn changed it twice (web `AssistantTaskReceipts`). Only successful
/// canonical write-tool receipts count; the receipt itself never shows.
class AssistantTaskReceipts extends ConsumerWidget {
  const AssistantTaskReceipts({
    super.key,
    required this.scope,
    required this.parts,
  });
  final AssistantScope scope;
  final List<MessagePart> parts;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final tasks = <String>{
      for (final part in parts)
        if (taskReceipt(part) case final receipt?) receipt.taskId,
    };
    if (tasks.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final taskId in tasks)
          AssistantTaskById(
            key: ValueKey(taskId),
            scope: scope,
            taskId: taskId,
          ),
      ],
    );
  }
}
