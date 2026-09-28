import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/cron.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/utils/format.dart';
import '../api/cron_api.dart';
import '../utils/schedule.dart';

/// Colour of a job's state dot: live, armed, tripped, or off (web `jobDotClass`).
Color cronJobDotColor(BossipTokens t, CronJob job) => job.running
    ? t.a700
    : job.enabled
    ? t.sage
    : job.autoDisabled
    ? t.danger
    : t.n400;

/// The job's state, as a dot and a word.
class CronStateDot extends ConsumerWidget {
  const CronStateDot({super.key, required this.job});

  final CronJob job;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final label = job.running
        ? i18n.t('cron:job.state.running')
        : job.enabled
        ? i18n.t('cron:job.state.enabled')
        : job.autoDisabled
        ? i18n.t('cron:job.state.autoDisabled')
        : i18n.t('cron:job.state.disabled');
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          width: 6,
          height: 6,
          decoration: BoxDecoration(
            color: cronJobDotColor(t, job),
            shape: BoxShape.circle,
          ),
        ),
        const SizedBox(width: 6),
        Text(
          label,
          style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
        ),
      ],
    );
  }
}

/// Run now / enable / edit / delete, shared by the list card and the task page.
class CronJobActions extends ConsumerStatefulWidget {
  const CronJobActions({
    super.key,
    required this.job,
    required this.onEdit,
    this.onDeleted,
  });

  final CronJob job;
  final void Function(CronJob job) onEdit;

  /// The task page leaves once its job is gone.
  final VoidCallback? onDeleted;

  @override
  ConsumerState<CronJobActions> createState() => _CronJobActionsState();
}

class _CronJobActionsState extends ConsumerState<CronJobActions> {
  bool _busy = false;

  Future<void> _act(Future<void> Function() action) async {
    setState(() => _busy = true);
    try {
      await action();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
    ref.invalidate(cronJobsProvider);
    ref.invalidate(cronStatusProvider);
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final job = widget.job;
    final api = ref.read(cronApiProvider);
    return Wrap(
      spacing: 6,
      runSpacing: 6,
      children: [
        _pill(
          t,
          i18n.t('cron:job.action.runNow'),
          enabled: !_busy && !job.running,
          onTap: () => _act(() => api.runNow(job.id)),
        ),
        _pill(
          t,
          job.enabled
              ? i18n.t('cron:job.action.disable')
              : i18n.t('cron:job.action.enable'),
          enabled: !_busy,
          onTap: () =>
              _act(() => api.update(job.id, {'enabled': !job.enabled})),
        ),
        _pill(
          t,
          i18n.t('cron:job.action.edit'),
          enabled: !_busy,
          onTap: () => widget.onEdit(job),
        ),
        _pill(
          t,
          i18n.t('cron:job.action.delete'),
          enabled: !_busy,
          danger: true,
          onTap: _confirmDelete,
        ),
      ],
    );
  }

  Widget _pill(
    BossipTokens t,
    String label, {
    required bool enabled,
    required VoidCallback onTap,
    bool danger = false,
  }) {
    return Opacity(
      opacity: enabled ? 1 : 0.5,
      child: OutlinedButton(
        onPressed: enabled ? onTap : null,
        style: OutlinedButton.styleFrom(
          side: BorderSide(color: t.hair),
          foregroundColor: danger ? t.danger : t.n800,
          minimumSize: const Size(0, 32),
          padding: const EdgeInsets.symmetric(horizontal: 12),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(Radii.full),
          ),
        ),
        child: Text(label, style: const TextStyle(fontSize: FontSizes.xs)),
      ),
    );
  }

  Future<void> _confirmDelete() async {
    final i18n = ref.read(i18nProvider);
    final t = context.tokens;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(
          i18n.t('cron:job.deleteConfirm.title'),
          style: const TextStyle(fontSize: FontSizes.lg),
        ),
        content: Text(
          i18n.t(
            'cron:job.deleteConfirm.body',
            vars: {'name': widget.job.name},
          ),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: Text(i18n.t('cron:form.cancel')),
          ),
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, true),
            child: Text(
              i18n.t('cron:job.action.delete'),
              style: TextStyle(color: t.danger),
            ),
          ),
        ],
      ),
    );
    if (confirmed == true) {
      await _act(() => ref.read(cronApiProvider).delete(widget.job.id));
      widget.onDeleted?.call();
    }
  }
}

/// Schedule / next / last / stats line, shared by the card and the task page.
class CronJobMeta extends ConsumerWidget {
  const CronJobMeta({super.key, required this.job});

  final CronJob job;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    Widget meta(String text) => Text(
      text,
      style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
    );
    return Wrap(
      spacing: 12,
      runSpacing: 4,
      children: [
        meta(describeSchedule(job.schedule, i18n.t)),
        if (job.enabled && job.nextRunAt != null)
          meta(
            i18n.t(
              'cron:job.nextRun',
              vars: {'when': formatRelative(job.nextRunAt!, i18n.language)},
            ),
          ),
        if (job.lastRunAt != null)
          meta(
            i18n.t(
              'cron:job.lastRun',
              vars: {'when': formatRelative(job.lastRunAt!, i18n.language)},
            ),
          ),
        meta(
          i18n.t(
            'cron:job.stats',
            vars: {
              'total': job.totalRuns,
              'ok': job.totalSuccesses,
              'failed': job.totalFailures,
            },
          ),
        ),
      ],
    );
  }
}

/// One scheduled job on the list page (web `CronJobCard`): name + state,
/// prompt excerpt, meta, actions. Its runs live on the task page.
class CronJobCard extends ConsumerWidget {
  const CronJobCard({super.key, required this.job, required this.onEdit});

  final CronJob job;
  final void Function(CronJob job) onEdit;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);

    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.lg),
        border: Border.all(color: t.hair),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          InkWell(
            key: ValueKey('cron-job-${job.id}'),
            borderRadius: const BorderRadius.vertical(
              top: Radius.circular(Radii.lg),
            ),
            onTap: () => context.push(Paths.cronJob(job.id)),
            child: Padding(
              padding: const EdgeInsets.fromLTRB(14, 12, 14, 8),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          job.name,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontSize: FontSizes.base,
                            color: t.ink,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      CronStateDot(job: job),
                      const SizedBox(width: 4),
                      Icon(Icons.chevron_right, size: 18, color: t.n500),
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(
                    job.taskPrompt,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: FontSizes.sm,
                      color: t.n700,
                      height: 1.5,
                    ),
                  ),
                  const SizedBox(height: 8),
                  CronJobMeta(job: job),
                  if ((job.lastError ?? '').isNotEmpty && !job.enabled)
                    Padding(
                      padding: const EdgeInsets.only(top: 6),
                      child: Text(
                        i18n.t('cron:job.lastError'),
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontSize: FontSizes.xs,
                          color: t.danger,
                        ),
                      ),
                    ),
                ],
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 0, 14, 12),
            child: CronJobActions(job: job, onEdit: onEdit),
          ),
        ],
      ),
    );
  }
}
