import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import '../api/assistant_api.dart';
import '../state/assistant_pending.dart';
import '../state/assistant_watch.dart';
import '../utils/task_status.dart';
import 'assistant_link_existing.dart';
import 'assistant_requests.dart';
import 'assistant_task_card.dart';

/// "我的任务" groups, in the order a secretary would read them out: what
/// needs you, what is still going, what finished lately.
final _groups = <(String, bool Function(TaskStatus))>[
  ('waiting', (status) => status == TaskStatus.waiting),
  ('active', (status) => isActiveTask(status) && status != TaskStatus.waiting),
  ('finished', (status) => !isActiveTask(status)),
];

/// The top-bar entry (web `AssistantTopbarActions`): a count of work still
/// going, and a red count when something waits on the user.
class AssistantTasksButton extends ConsumerWidget {
  const AssistantTasksButton({super.key, required this.scope});
  final AssistantScope scope;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (ref.watch(assistantScopeProvider) != scope) {
      return const SizedBox.shrink();
    }
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final items =
        ref.watch(assistantWatchProvider(scope)).valueOrNull?.items ??
        const <AssistantWatchItem>[];
    final statuses = items.map((item) => item.status).toList();
    final active = statuses.where(isActiveTask).length;
    final pending = ref.watch(assistantPendingProvider(scope)).valueOrNull;
    final waiting =
        (pending?.count ?? 0) +
        items
            .where(
              (item) =>
                  item.status == TaskStatus.waiting &&
                  !(pending?.sessions.contains(item.sessionId) ?? false),
            )
            .fold<int>(
              0,
              (total, item) =>
                  total +
                  (item.pendingQuestions > 0 ? item.pendingQuestions : 1),
            );
    final label = waiting > 0
        ? i18n.t(
            'chat:assistant.taskList.buttonWaiting',
            vars: {'count': waiting},
          )
        : i18n.t('chat:assistant.taskList.button');
    // Leave room for the assistant title and call action on phone toolbars.
    final pendingLabel = MediaQuery.sizeOf(context).width < 480
        ? '$waiting'
        : i18n.t(
            'chat:assistant.taskList.pendingCount',
            vars: {'count': waiting},
          );
    void open() => showAssistantTasksSheet(context, scope);
    return Semantics(
      button: true,
      label: label,
      onTap: open,
      excludeSemantics: true,
      child: Padding(
        // A pill needs the margin an icon button gets from its own padding.
        padding: const EdgeInsets.fromLTRB(4, 10, 12, 10),
        child: Stack(
          clipBehavior: Clip.none,
          children: [
            Material(
              color: Colors.transparent,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(Radii.full),
                side: BorderSide(color: t.hair),
              ),
              clipBehavior: Clip.antiAlias,
              child: InkWell(
                key: const ValueKey('assistant-tasks-button'),
                onTap: open,
                child: Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 11),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(Icons.checklist_rounded, size: 16, color: t.n800),
                      const SizedBox(width: 5),
                      Text(
                        i18n.t('chat:assistant.taskList.title'),
                        style: TextStyle(fontSize: FontSizes.sm, color: t.n800),
                      ),
                      if (active > 0) ...[
                        const SizedBox(width: 5),
                        Container(
                          padding: const EdgeInsets.symmetric(horizontal: 6),
                          decoration: BoxDecoration(
                            color: t.hairSoft,
                            borderRadius: BorderRadius.circular(Radii.full),
                          ),
                          child: Text(
                            '$active',
                            style: TextStyle(
                              fontSize: FontSizes.xs,
                              height: 1.6,
                              color: t.n800,
                            ),
                          ),
                        ),
                      ],
                      if (waiting > 0) ...[
                        const SizedBox(width: 5),
                        Text(
                          '$pendingLabel${pending?.hasMore == true ? '+' : ''}',
                          key: const ValueKey('assistant-tasks-pending-count'),
                          style: TextStyle(
                            fontSize: FontSizes.xs,
                            color: t.dangerInk,
                            fontWeight: FontWeight.w500,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
              ),
            ),
            if (waiting > 0)
              Positioned(
                right: -1,
                top: -1,
                child: Container(
                  key: const ValueKey('assistant-tasks-waiting-dot'),
                  width: 10,
                  height: 10,
                  decoration: BoxDecoration(
                    color: t.dangerInk,
                    shape: BoxShape.circle,
                    border: Border.all(color: t.bg, width: 2),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

Future<void> showAssistantTasksSheet(
  BuildContext context,
  AssistantScope scope,
) {
  FocusScope.of(context).unfocus();
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    backgroundColor: context.tokens.bg,
    builder: (_) => AssistantTasksSheet(scope: scope),
  );
}

/// Everything handed to the personal assistant, in one sheet (web
/// `AssistantTasksSheet`): waiting-on-you first, then work in progress, then
/// what finished lately. Following an existing conversation lives at its
/// foot.
class AssistantTasksSheet extends ConsumerWidget {
  const AssistantTasksSheet({super.key, required this.scope});
  final AssistantScope scope;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final watch = ref.watch(assistantWatchProvider(scope));
    final pending = ref.watch(assistantPendingProvider(scope)).valueOrNull;
    final current = ref.watch(assistantScopeProvider) == scope;
    return DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.85,
      minChildSize: 0.4,
      maxChildSize: 0.95,
      builder: (sheetContext, controller) => Column(
        children: [
          _SheetHeader(
            title: i18n.t('chat:assistant.taskList.title'),
            description: i18n.t('chat:assistant.taskList.description'),
            closeLabel: i18n.t('chat:assistant.taskList.close'),
          ),
          Expanded(
            child: !current
                ? const SizedBox.shrink()
                : ListView(
                    controller: controller,
                    padding: const EdgeInsets.fromLTRB(16, 4, 16, 16),
                    children: [
                      AssistantRequests(
                        scope: scope,
                        beforeOpen: () => Navigator.of(sheetContext).pop(),
                      ),
                      if (watch.hasError)
                        Padding(
                          padding: const EdgeInsets.symmetric(vertical: 24),
                          child: Text(
                            errorText(i18n, watch.error!),
                            textAlign: TextAlign.center,
                            style: TextStyle(
                              fontSize: FontSizes.sm,
                              color: t.dangerInk,
                            ),
                          ),
                        )
                      else if (!watch.hasValue)
                        const Padding(
                          padding: EdgeInsets.symmetric(vertical: 40),
                          child: Center(
                            child: SizedBox.square(
                              dimension: 20,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            ),
                          ),
                        )
                      else if (watch.value!.items.isEmpty &&
                          (pending?.count ?? 0) == 0)
                        _Empty(
                          title: i18n.t('chat:assistant.taskList.emptyTitle'),
                          hint: i18n.t('chat:assistant.taskList.emptyHint'),
                        )
                      else ...[
                        for (final (key, match) in _groups)
                          if (watch.value!.items
                                  .where((item) => match(item.status))
                                  .toList()
                              case final rows when rows.isNotEmpty)
                            Semantics(
                              container: true,
                              label: i18n.t(
                                'chat:assistant.taskList.groups.$key',
                              ),
                              child: Padding(
                                padding: const EdgeInsets.only(bottom: 14),
                                child: Column(
                                  crossAxisAlignment:
                                      CrossAxisAlignment.stretch,
                                  children: [
                                    Padding(
                                      padding: const EdgeInsets.only(
                                        bottom: 2,
                                        top: 4,
                                      ),
                                      child: Text(
                                        '${i18n.t('chat:assistant.taskList.groups.$key')} · ${rows.length}',
                                        style: TextStyle(
                                          fontSize: FontSizes.sm,
                                          fontWeight: FontWeight.w500,
                                          color: key == 'waiting'
                                              ? t.dangerInk
                                              : t.n600,
                                        ),
                                      ),
                                    ),
                                    for (final item in rows)
                                      AssistantTaskById(
                                        key: ValueKey(item.taskId),
                                        scope: scope,
                                        taskId: item.taskId,
                                        beforeOpen: () =>
                                            Navigator.of(sheetContext).pop(),
                                      ),
                                  ],
                                ),
                              ),
                            ),
                        if (watch.value!.hasMore)
                          Text(
                            i18n.t('chat:assistant.taskList.more'),
                            style: TextStyle(
                              fontSize: FontSizes.sm,
                              height: 1.6,
                              color: t.n600,
                            ),
                          ),
                      ],
                    ],
                  ),
          ),
          DecoratedBox(
            decoration: BoxDecoration(
              border: Border(top: BorderSide(color: t.hair)),
            ),
            child: SafeArea(
              top: false,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(8, 4, 8, 4),
                child: Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton.icon(
                    onPressed: current
                        ? () => showAssistantLinkSheet(context, scope)
                        : null,
                    icon: Icon(Icons.add, size: 17, color: t.n800),
                    label: Text(
                      i18n.t('chat:assistant.taskList.followExisting'),
                      style: TextStyle(fontSize: FontSizes.sm, color: t.n800),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Hand an existing conversation to the assistant (web: the drawer's
/// "follow existing" dialog).
Future<void> showAssistantLinkSheet(
  BuildContext context,
  AssistantScope scope,
) => showModalBottomSheet<void>(
  context: context,
  isScrollControlled: true,
  useSafeArea: true,
  builder: (_) => Consumer(
    builder: (context, ref, _) {
      final i18n = ref.watch(i18nProvider);
      return DraggableScrollableSheet(
        expand: false,
        initialChildSize: 0.75,
        minChildSize: 0.4,
        maxChildSize: 0.95,
        builder: (context, controller) => Column(
          children: [
            _SheetHeader(
              title: i18n.t('chat:assistant.link.title'),
              closeLabel: i18n.t('common:action.close'),
            ),
            Expanded(
              child: SingleChildScrollView(
                controller: controller,
                padding: const EdgeInsets.fromLTRB(16, 4, 16, 24),
                child: AssistantLinkExisting(scope: scope),
              ),
            ),
          ],
        ),
      );
    },
  ),
);

class _SheetHeader extends StatelessWidget {
  const _SheetHeader({
    required this.title,
    required this.closeLabel,
    this.description,
  });
  final String title;
  final String closeLabel;
  final String? description;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Column(
      children: [
        Container(
          margin: const EdgeInsets.only(top: 8),
          width: 36,
          height: 4,
          decoration: BoxDecoration(
            color: t.n400,
            borderRadius: BorderRadius.circular(Radii.full),
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 6, 6, 8),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: Padding(
                  padding: const EdgeInsets.only(top: 10),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Semantics(
                        header: true,
                        child: Text(
                          title,
                          style: TextStyle(
                            fontSize: FontSizes.xl,
                            fontWeight: FontWeight.w500,
                            color: t.ink,
                          ),
                        ),
                      ),
                      if (description != null) ...[
                        const SizedBox(height: 4),
                        Text(
                          description!,
                          style: TextStyle(
                            fontSize: FontSizes.sm,
                            height: 1.5,
                            color: t.n600,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
              ),
              IconButton(
                tooltip: closeLabel,
                icon: Icon(Icons.close, size: 20, color: t.n700),
                onPressed: () => Navigator.of(context).pop(),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

class _Empty extends StatelessWidget {
  const _Empty({required this.title, required this.hint});
  final String title;
  final String hint;
  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 40, horizontal: 12),
      child: Column(
        children: [
          Text(
            title,
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: FontSizes.base, color: t.ink),
          ),
          const SizedBox(height: 8),
          Text(
            hint,
            textAlign: TextAlign.center,
            style: TextStyle(
              fontSize: FontSizes.sm,
              height: 1.6,
              color: t.n600,
            ),
          ),
        ],
      ),
    );
  }
}
