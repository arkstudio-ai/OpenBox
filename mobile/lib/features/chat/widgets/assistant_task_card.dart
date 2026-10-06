import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/toast.dart';
import '../api/assistant_api.dart';
import '../state/assistant_controller.dart';
import '../state/assistant_watch.dart';
import '../utils/task_status.dart';
import 'assistant_report.dart';

typedef AssistantTaskKey = ({AssistantScope scope, String id});

/// A task's current projection: the main view's copy when it has one, else a
/// scoped read. A failed or refreshed projection cannot keep displaying an
/// old authority.
final assistantTaskProvider = FutureProvider.autoDispose
    .family<AssistantTask, AssistantTaskKey>((ref, key) {
      final tasks = ref.watch(
        assistantControllerProvider(key.scope).select((s) => s.tasks),
      );
      final task = tasks.where((t) => t.id == key.id).firstOrNull;
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

/// A followed task loaded by id, as a card: a quiet line while it loads and
/// a plain sentence when it cannot be read.
class AssistantTaskById extends ConsumerWidget {
  const AssistantTaskById({
    super.key,
    required this.scope,
    required this.taskId,
    this.beforeOpen,
  });
  final AssistantScope scope;
  final String taskId;
  final VoidCallback? beforeOpen;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (ref.watch(assistantScopeProvider) != scope) {
      return const SizedBox.shrink();
    }
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final query = ref.watch(assistantTaskProvider((scope: scope, id: taskId)));
    return query.when(
      skipLoadingOnRefresh: false,
      loading: () => _CardShell(
        child: Text(
          i18n.t('chat:assistant.card.loading'),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
        ),
      ),
      error: (error, _) => _CardShell(
        child: Text(
          // A deleted task, conversation or project: say so, not an error.
          _gone.contains(apiErrorOf(error)?.code)
              ? i18n.t('chat:assistant.card.gone')
              : errorText(i18n, error),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
        ),
      ),
      data: (task) => AssistantTaskCard(
        key: ValueKey(task.id),
        task: task,
        scope: scope,
        beforeOpen: beforeOpen,
      ),
    );
  }
}

/// The task, its conversation or its project was deleted (web `GONE`).
const _gone = {
  'ASSISTANT_EXECUTION_UNAVAILABLE',
  'ASSISTANT_TASK_UNAVAILABLE',
  'ASSISTANT_PROJECT_UNAVAILABLE',
};

/// A task the personal assistant follows, as one plain card (web
/// `AssistantTaskCard`): what it is, where it runs, whether it needs you,
/// and the latest word from it. Controls sit behind "更多"; ids and receipts
/// never reach the page.
class AssistantTaskCard extends ConsumerStatefulWidget {
  const AssistantTaskCard({
    super.key,
    required this.task,
    required this.scope,
    this.selectedResult,
    this.beforeOpen,
  });
  final AssistantTask task;
  final AssistantScope scope;

  /// A notification can point at an older run; that result stays selected.
  final Map<String, dynamic>? selectedResult;

  /// Runs before the conversation opens, e.g. to close the sheet the card
  /// sits in.
  final VoidCallback? beforeOpen;

  @override
  ConsumerState<AssistantTaskCard> createState() => _AssistantTaskCardState();
}

class _AssistantTaskCardState extends ConsumerState<AssistantTaskCard> {
  bool _busy = false;
  bool _showResult = false;

  AssistantController get _controller =>
      ref.read(assistantControllerProvider(widget.scope).notifier);

  Map<String, dynamic> get _result =>
      widget.selectedResult ?? widget.task.result;

  /// One action at a time; the outcome is said in plain words.
  Future<void> _run(Future<void> Function() action, {String? done}) async {
    if (_busy) return;
    setState(() => _busy = true);
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    try {
      await action();
      if (done != null && mounted) toast.info(i18n.t(done));
    } catch (error) {
      if (mounted) {
        final api = apiErrorOf(error);
        if (api?.code == 'ASSISTANT_EFFECT_UNRESOLVED') {
          toast.info(i18n.t('chat:assistant.card.checkEffects'));
        } else if (api?.code == 'ASSISTANT_SEND_UNCERTAIN') {
          toast.error(i18n.t('chat:assistant.sendUncertain'));
        } else if (api?.status == 409) {
          toast.info(i18n.t('chat:assistant.card.changed'));
        } else {
          toast.error(errorText(i18n, error));
        }
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _select(String choice, String? pending) {
    final task = widget.task;
    switch (choice) {
      case 'result':
        setState(() => _showResult = !_showResult);
      case 'retry':
        // The report of the result on this card, which a notification may
        // have selected over the latest one.
        _run(
          () => _controller.retryReport(
            AssistantTask({...task.data, 'latest_result': _result}),
          ),
        );
      case 'pending':
        if (pending != null) {
          _run(
            () => _controller.control(task, pending),
            done: 'chat:assistant.card.controlDone.$pending',
          );
        }
      case 'pause' || 'resume' || 'cancel':
        _run(
          () => _controller.control(task, choice),
          done: 'chat:assistant.card.controlDone.$choice',
        );
      case 'unfollow':
        _run(
          () => _controller.archive(task),
          done: 'chat:assistant.card.unfollowed',
        );
    }
  }

  void _open() {
    final router = GoRouter.of(context);
    widget.beforeOpen?.call();
    router.push(Paths.chat(widget.task.executionId));
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final task = widget.task;
    final result = _result;
    final watched = ref
        .watch(assistantWatchProvider(widget.scope))
        .valueOrNull
        ?.byTask(task.id);
    final pending = ref.watch(
      assistantControllerProvider(
        widget.scope,
      ).select((s) => s.actionPending[task.id]),
    );
    final status = taskStatus(
      sessionStatus: task.sessionStatus,
      observedState: task.observed,
      desiredState: task.desired,
      pendingQuestions: watched?.pendingQuestions ?? 0,
      outcome: asString(result['outcome']),
    );
    // A notification can point at an older run; say so rather than pass it
    // off as the latest.
    final selected = widget.selectedResult;
    final earlier =
        selected != null && selected['result_id'] != task.result['result_id'];
    final summary = selected == null ? watched?.summary : null;
    final note = earlier
        ? 'chat:assistant.card.note.earlier'
        : _note(task, result);
    final continuation = task.continuation;
    final following =
        continuation['state'] == 'active' && task.desired != 'paused';
    final decision = const {
      'needs_decision',
      'exhausted',
    }.contains(continuation['state']);
    final meta = [
      ?watched?.projectName,
      sinceLabel(task.updatedAt, i18n.language),
    ].where((part) => part.isNotEmpty).join(' · ');
    final body = TextStyle(fontSize: FontSizes.sm, height: 1.5, color: t.n800);

    return Semantics(
      container: true,
      label: i18n.t('chat:assistant.card.label', vars: {'title': task.title}),
      child: _CardShell(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsets.only(top: 7, right: 10),
                  child: _StatusDot(status: status),
                ),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Wrap(
                        spacing: 8,
                        runSpacing: 4,
                        crossAxisAlignment: WrapCrossAlignment.center,
                        children: [
                          Text(
                            task.title,
                            style: TextStyle(
                              fontSize: FontSizes.base,
                              fontWeight: FontWeight.w500,
                              color: t.ink,
                            ),
                          ),
                          _StatusPill(
                            status: status,
                            label: i18n.t(
                              'chat:assistant.status.${status.name}',
                            ),
                          ),
                        ],
                      ),
                      if (meta.isNotEmpty)
                        Padding(
                          padding: const EdgeInsets.only(top: 2),
                          child: Text(
                            meta,
                            style: TextStyle(
                              fontSize: FontSizes.sm,
                              color: t.n600,
                            ),
                          ),
                        ),
                      if (summary != null)
                        Padding(
                          padding: const EdgeInsets.only(top: 8),
                          child: Text(
                            summary,
                            maxLines: 3,
                            overflow: TextOverflow.ellipsis,
                            style: body,
                          ),
                        ),
                      if (status == TaskStatus.waiting)
                        _Line(
                          i18n.t('chat:assistant.card.waitingHint'),
                          style: body,
                        ),
                      if (following)
                        _Line(
                          i18n.t(
                            'chat:assistant.card.followingUp',
                            vars: {
                              'used': asInt(continuation['followups_used']),
                              'limit': asInt(continuation['max_followups']),
                            },
                          ),
                          style: TextStyle(
                            fontSize: FontSizes.xs,
                            color: t.n600,
                          ),
                        ),
                      if (decision)
                        _Line(
                          i18n.t(
                            'chat:assistant.card.continuation.${continuation['state']}',
                          ),
                          style: body,
                        ),
                      if (note != null)
                        _Line(
                          i18n.t(note),
                          style: TextStyle(
                            fontSize: FontSizes.sm,
                            height: 1.5,
                            color: t.n700,
                          ),
                        ),
                    ],
                  ),
                ),
                _TaskMenu(
                  busy: _busy,
                  items: _menuItems(i18n, task, result, pending),
                  onSelected: (choice) => _select(choice, pending),
                ),
              ],
            ),
            Padding(
              padding: const EdgeInsets.only(left: 18, top: 10),
              child: Align(
                alignment: Alignment.centerLeft,
                child: _OpenButton(
                  label: i18n.t(
                    status == TaskStatus.waiting
                        ? 'chat:assistant.card.answer'
                        : 'chat:assistant.card.open',
                  ),
                  onTap: _open,
                ),
              ),
            ),
            if (_showResult && result['result_id'] is String)
              Padding(
                padding: const EdgeInsets.only(left: 18),
                child: AssistantFullResult(
                  key: ValueKey(result['result_id']),
                  scope: widget.scope,
                  resultId: result['result_id'] as String,
                ),
              ),
          ],
        ),
      ),
    );
  }

  List<(String, String, bool)> _menuItems(
    I18nState i18n,
    AssistantTask task,
    Map<String, dynamic> result,
    String? pending,
  ) {
    final hasResult = result['result_id'] is String;
    final canRetry =
        hasResult &&
        const {'blocked', 'retry_wait'}.contains(result['delivery_state']);
    final canPause = task.desired == 'running' && !task.archived;
    final canResume =
        task.desired == 'paused' &&
        const {'paused', 'resume_blocked'}.contains(task.observed);
    final canCancel = task.desired != 'canceled' && !task.archived;
    return [
      if (hasResult)
        (
          'result',
          i18n.t(
            _showResult
                ? 'chat:assistant.card.hideResult'
                : 'chat:assistant.card.showResult',
          ),
          false,
        ),
      if (canRetry) ('retry', i18n.t('chat:assistant.card.retryReport'), false),
      // A control whose answer was lost is sent again as it was, before
      // anything else can be asked of the task.
      if (pending != null && _controlKeys.containsKey(pending))
        (
          'pending',
          '${i18n.t('chat:assistant.reload')} · ${i18n.t(_controlKeys[pending]!)}',
          pending == 'cancel',
        )
      else ...[
        if (canPause) ('pause', i18n.t('chat:assistant.card.pause'), false),
        if (canResume) ('resume', i18n.t('chat:assistant.card.resume'), false),
        if (canCancel) ('cancel', i18n.t('chat:assistant.card.cancel'), true),
      ],
      if (!task.archived)
        ('unfollow', i18n.t('chat:assistant.card.unfollow'), false),
    ];
  }
}

const _controlKeys = {
  'pause': 'chat:assistant.card.pause',
  'resume': 'chat:assistant.card.resume',
  'cancel': 'chat:assistant.card.cancel',
};

/// A note under the summary only when something needs explaining.
String? _note(AssistantTask task, Map<String, dynamic> result) {
  final submission = task.submission;
  if (task.observed == 'effect_unknown') {
    return 'chat:assistant.card.note.effectUnknown';
  }
  if (task.observed == 'resume_blocked') {
    return 'chat:assistant.card.note.resumeBlocked';
  }
  if (result['delivery_state'] == 'blocked') {
    return result['last_error_code'] == 'user_stopped'
        ? 'chat:assistant.card.note.reportStopped'
        : 'chat:assistant.card.note.reportFailed';
  }
  if (submission['disposition'] == 'not_applied') {
    return 'chat:assistant.card.note.steerNotApplied';
  }
  if (submission['state'] == 'canceled' && submission['applied_at'] == null) {
    return asMap(submission['error'])['code'] == 'ASSISTANT_ASSET_UNAVAILABLE'
        ? 'chat:assistant.card.note.assetUnavailable'
        : 'chat:assistant.card.note.inputCanceled';
  }
  return null;
}

class _CardShell extends StatelessWidget {
  const _CardShell({required this.child});
  final Widget child;
  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.symmetric(vertical: 6),
      padding: const EdgeInsets.fromLTRB(14, 12, 6, 14),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.xl),
        border: Border.all(color: t.hair),
      ),
      child: child,
    );
  }
}

class _Line extends StatelessWidget {
  const _Line(this.text, {required this.style});
  final String text;
  final TextStyle style;
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(top: 8),
    child: Text(text, style: style),
  );
}

class _StatusDot extends StatelessWidget {
  const _StatusDot({required this.status});
  final TaskStatus status;
  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final color = switch (status) {
      TaskStatus.waiting || TaskStatus.running => t.accent,
      TaskStatus.queued => t.a300,
      TaskStatus.done => t.sage,
      TaskStatus.failed => t.danger,
      TaskStatus.paused || TaskStatus.stopped || TaskStatus.idle => t.n400,
    };
    return Container(
      width: 8,
      height: 8,
      decoration: BoxDecoration(color: color, shape: BoxShape.circle),
    );
  }
}

/// One status, in one tone (web `StatusPill`).
class _StatusPill extends StatelessWidget {
  const _StatusPill({required this.status, required this.label});
  final TaskStatus status;
  final String label;
  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final (background, foreground) = switch (taskTone(status)) {
      TaskTone.ok => (t.s100, t.s800),
      TaskTone.warn => (t.a200, t.n800),
      TaskTone.danger => (t.dangerSoft, t.dangerInk),
      TaskTone.muted => (t.hairSoft, t.n700),
      TaskTone.accent => (t.a100, t.a700),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(Radii.full),
      ),
      child: Text(
        label,
        style: TextStyle(fontSize: FontSizes.xs, color: foreground),
      ),
    );
  }
}

class _TaskMenu extends ConsumerWidget {
  const _TaskMenu({
    required this.busy,
    required this.items,
    required this.onSelected,
  });
  final bool busy;
  final List<(String, String, bool)> items;
  final ValueChanged<String> onSelected;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return PopupMenuButton<String>(
      enabled: !busy && items.isNotEmpty,
      tooltip: i18n.t('chat:assistant.card.more'),
      icon: Icon(Icons.more_horiz, size: 20, color: t.n700),
      padding: EdgeInsets.zero,
      onSelected: onSelected,
      itemBuilder: (_) => [
        for (final (value, label, danger) in items)
          PopupMenuItem<String>(
            value: value,
            child: Text(
              label,
              style: TextStyle(
                fontSize: FontSizes.base,
                color: danger ? t.dangerInk : t.ink,
              ),
            ),
          ),
      ],
    );
  }
}

class _OpenButton extends StatelessWidget {
  const _OpenButton({required this.label, required this.onTap});
  final String label;
  final VoidCallback onTap;
  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: Colors.transparent,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(Radii.full),
        side: BorderSide(color: t.hair),
      ),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                label,
                style: TextStyle(fontSize: FontSizes.sm, color: t.n800),
              ),
              const SizedBox(width: 4),
              Icon(Icons.north_east, size: 13, color: t.n800),
            ],
          ),
        ),
      ),
    );
  }
}
