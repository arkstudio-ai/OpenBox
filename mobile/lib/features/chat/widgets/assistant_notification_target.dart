import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/utils/error_text.dart';
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

/// The notification's original result stays selected when newer runs finish
/// (web `AssistantNotificationTarget`).
class AssistantNotificationTarget extends ConsumerWidget {
  const AssistantNotificationTarget({
    super.key,
    required this.scope,
    required this.taskId,
    required this.resultId,
  });
  final AssistantScope scope;
  final String taskId;
  final String resultId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    if (ref.watch(assistantScopeProvider) != scope) {
      return const SizedBox.shrink();
    }
    final query = ref.watch(
      _targetProvider((scope: scope, resultId: resultId)),
    );
    final muted = TextStyle(fontSize: FontSizes.sm, color: t.n700);
    return ConstrainedBox(
      constraints: const BoxConstraints(maxHeight: 300),
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 0),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              i18n.t('chat:assistant.notificationResult'),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
            query.when(
              skipLoadingOnRefresh: true,
              skipLoadingOnReload: true,
              loading: () => Padding(
                padding: const EdgeInsets.symmetric(vertical: 6),
                child: Text(
                  i18n.t('chat:assistant.card.loading'),
                  style: muted,
                ),
              ),
              error: (error, _) => Padding(
                padding: const EdgeInsets.symmetric(vertical: 6),
                child: Text(errorText(i18n, error), style: muted),
              ),
              data: (value) {
                final task = AssistantTask(asMap(value['task']));
                final result = asMap(value['result']);
                if (task.id != taskId || result['result_id'] != resultId) {
                  return Padding(
                    padding: const EdgeInsets.symmetric(vertical: 6),
                    child: Text(
                      i18n.t('chat:assistant.notificationUnavailable'),
                      style: muted,
                    ),
                  );
                }
                return AssistantTaskCard(
                  key: ValueKey(resultId),
                  task: task,
                  scope: scope,
                  selectedResult: result,
                );
              },
            ),
          ],
        ),
      ),
    );
  }
}
