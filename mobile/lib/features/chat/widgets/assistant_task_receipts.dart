import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/message_part.dart';
import '../api/assistant_api.dart';
import '../state/assistant_controller.dart';
import '../utils/task_receipt.dart';
import 'assistant_task_card.dart';

typedef _TaskKey = ({AssistantScope scope, String id});
final _taskProvider = FutureProvider.autoDispose
    .family<AssistantTask, _TaskKey>((ref, key) {
      final state = ref.watch(assistantControllerProvider(key.scope));
      final task = state.tasks.where((t) => t.id == key.id).firstOrNull;
      if (task != null) return task;
      return ref.read(assistantApiProvider(key.scope)).task(key.id).then((
        value,
      ) {
        if (value.id != key.id) {
          throw const FormatException('Unexpected task projection');
        }
        return value;
      });
    });

class AssistantTaskReceipts extends ConsumerWidget {
  const AssistantTaskReceipts({
    super.key,
    required this.scope,
    required this.parts,
    required this.onAction,
  });
  final AssistantScope scope;
  final List<MessagePart> parts;
  final Future<void> Function(Future<void> Function()) onAction;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final receipts = <String, TaskReceipt>{};
    for (final part in parts) {
      final receipt = taskReceipt(part);
      if (receipt != null) receipts[receipt.commandId] = receipt;
    }
    return Column(
      children: [
        for (final receipt in receipts.values)
          _ReceiptCard(
            key: ValueKey(receipt.commandId),
            scope: scope,
            receipt: receipt,
            onAction: onAction,
          ),
      ],
    );
  }
}

class _ReceiptCard extends ConsumerWidget {
  const _ReceiptCard({
    super.key,
    required this.scope,
    required this.receipt,
    required this.onAction,
  });
  final AssistantScope scope;
  final TaskReceipt receipt;
  final Future<void> Function(Future<void> Function()) onAction;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (ref.watch(assistantScopeProvider) != scope) {
      return const SizedBox.shrink();
    }
    final state = ref.watch(assistantControllerProvider(scope));
    final i18n = ref.watch(i18nProvider);
    final query = ref.watch(_taskProvider((scope: scope, id: receipt.taskId)));
    final controller = ref.read(assistantControllerProvider(scope).notifier);
    // A failed or refreshed projection cannot keep displaying an old authority.
    return query.when(
      skipLoadingOnRefresh: false,
      loading: () => Text(i18n.t('chat:assistant.loadingTask')),
      error: (_, _) => Text(i18n.t('chat:assistant.sourceUnavailable')),
      data: (task) => AssistantTaskCard(
        task: task,
        scope: scope,
        commandId: receipt.commandId,
        lastSeen: state.snapshot?.lastSeen ?? 0,
        pending: state.actionPending[task.id],
        onControl: (action) => onAction(() => controller.control(task, action)),
        onRetry: () => onAction(() => controller.retryReport(task)),
      ),
    );
  }
}
