import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../api/assistant_api.dart';
import '../state/assistant_pending.dart';
import 'assistant_tasks.dart';

/// Dismissing the compact hint never answers or cancels a pending request.
class AssistantRequestReminder extends ConsumerWidget {
  const AssistantRequestReminder({super.key, required this.scope});
  final AssistantScope scope;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (ref.watch(assistantScopeProvider) != scope) {
      return const SizedBox.shrink();
    }
    final pending = ref.watch(assistantPendingProvider(scope));
    final data = pending.valueOrNull;
    final ids = [...?data?.ids, if (pending.hasError) 'error'];
    final dismissed = ref.watch(assistantDismissedReminderProvider(scope));
    if (ids.isEmpty || ids.every(dismissed.contains)) {
      return const SizedBox.shrink();
    }
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final count = data?.count ?? 0;
    return Container(
      key: const ValueKey('assistant-request-reminder'),
      padding: const EdgeInsets.only(left: 12, right: 4),
      decoration: BoxDecoration(
        color: t.a100,
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.lg),
      ),
      child: Row(
        children: [
          Icon(
            Icons.notifications_active_outlined,
            size: 16,
            color: t.dangerInk,
          ),
          const SizedBox(width: 6),
          Expanded(
            child: TextButton(
              onPressed: () => showAssistantTasksSheet(context, scope),
              child: Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  count > 0
                      ? i18n.t(
                          'chat:assistant.requests.reminder',
                          vars: {'count': count},
                        )
                      : i18n.t('chat:assistant.requests.reviewTasks'),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.dangerInk),
                ),
              ),
            ),
          ),
          IconButton(
            tooltip: i18n.t('chat:assistant.requests.dismissReminder'),
            icon: Icon(Icons.close, size: 18, color: t.n600),
            onPressed: () {
              ref
                      .read(assistantDismissedReminderProvider(scope).notifier)
                      .state =
                  ids;
              unawaited(
                ref
                    .read(prefsProvider)
                    .setStringList(reminderStorageKey(scope), ids)
                    .catchError((_) => false),
              );
            },
          ),
        ],
      ),
    );
  }
}
