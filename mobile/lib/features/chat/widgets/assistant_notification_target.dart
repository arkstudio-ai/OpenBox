import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../api/assistant_api.dart';
import '../state/assistant_controller.dart';
import 'assistant_task_card.dart';

typedef _Target = ({AssistantScope scope, String resultId});
final _targetProvider = FutureProvider.autoDispose
    .family<Map<String, dynamic>, _Target>((ref, key) {
      // The existing foreground refresh/event loop also invalidates this target.
      ref.watch(assistantControllerProvider(key.scope));
      return ref
          .read(assistantApiProvider(key.scope))
          .resultTarget(key.resultId);
    });

class AssistantNotificationTarget extends ConsumerWidget {
  const AssistantNotificationTarget({
    super.key,
    required this.scope,
    required this.taskId,
    required this.resultId,
    required this.onAction,
  });
  final AssistantScope scope;
  final String taskId;
  final String resultId;
  final Future<void> Function(Future<void> Function()) onAction;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    if (ref.watch(assistantScopeProvider) != scope) {
      return const SizedBox.shrink();
    }
    final state = ref.watch(assistantControllerProvider(scope));
    final controller = ref.read(assistantControllerProvider(scope).notifier);
    final query = ref.watch(
      _targetProvider((scope: scope, resultId: resultId)),
    );
    return ConstrainedBox(
      constraints: const BoxConstraints(maxHeight: 260),
      child: SingleChildScrollView(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(i18n.t('chat:assistant.notificationResult')),
            query.when(
              skipLoadingOnRefresh: true,
              skipLoadingOnReload: true,
              loading: () => Text(i18n.t('chat:assistant.loadingTask')),
              error: (_, _) => Text(i18n.t('chat:assistant.sourceUnavailable')),
              data: (value) {
                final task = AssistantTask(asMap(value['task']));
                final result = asMap(value['result']);
                if (task.id != taskId || result['result_id'] != resultId) {
                  return Text(i18n.t('chat:assistant.sourceUnavailable'));
                }
                return AssistantTaskCard(
                  key: ValueKey(resultId),
                  task: task,
                  scope: scope,
                  selectedResult: result,
                  lastSeen: state.snapshot?.lastSeen ?? 0,
                  pending: state.actionPending[task.id],
                  onControl: (action) =>
                      onAction(() => controller.control(task, action)),
                  onRetry: () => onAction(
                    () => controller.retryReport(
                      AssistantTask({...task.data, 'latest_result': result}),
                    ),
                  ),
                );
              },
            ),
          ],
        ),
      ),
    );
  }
}
