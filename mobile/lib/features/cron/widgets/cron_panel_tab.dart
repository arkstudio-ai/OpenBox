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
import 'cron_run_list.dart';

/// The workbench "定时任务" tab (web `CronPanelTab`): the current project's
/// jobs, compact rows with run-now/toggle + expandable history.
class CronPanelTab extends ConsumerWidget {
  const CronPanelTab({super.key, required this.projectId});

  final String? projectId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final jobs = ref.watch(cronJobsProvider);

    return jobs.when(
      loading: () => Center(
        child: Text(i18n.t('cron:page.loading'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600)),
      ),
      error: (_, _) => Center(
        child: Text(i18n.t('cron:page.loadFailed'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.danger)),
      ),
      data: (list) {
        final mine = list
            .where((j) => j.projectId != null && j.projectId == projectId)
            .toList();
        return ListView(
          padding: const EdgeInsets.fromLTRB(14, 6, 14, 16),
          children: [
            if (projectId == null || mine.isEmpty)
              Container(
                padding: const EdgeInsets.fromLTRB(14, 16, 14, 16),
                decoration: BoxDecoration(
                  color: t.card,
                  borderRadius: BorderRadius.circular(Radii.lg),
                  border: Border.all(color: t.hair),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(i18n.t('cron:panel.empty.title'),
                        style: TextStyle(
                            fontSize: FontSizes.sm, color: t.ink)),
                    const SizedBox(height: 4),
                    Text(i18n.t('cron:panel.empty.body'),
                        style: TextStyle(
                            fontSize: FontSizes.xs,
                            color: t.n600,
                            height: 1.6)),
                  ],
                ),
              ),
            for (final job in mine) _PanelJobRow(job: job),
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: GestureDetector(
                onTap: () => context.push(Paths.cron),
                child: Text(
                  i18n.t('cron:panel.manageAll'),
                  style: TextStyle(
                    fontSize: FontSizes.xs,
                    color: t.n600,
                    decoration: TextDecoration.underline,
                  ),
                ),
              ),
            ),
          ],
        );
      },
    );
  }
}

class _PanelJobRow extends ConsumerStatefulWidget {
  const _PanelJobRow({required this.job});

  final CronJob job;

  @override
  ConsumerState<_PanelJobRow> createState() => _PanelJobRowState();
}

class _PanelJobRowState extends ConsumerState<_PanelJobRow> {
  bool _expanded = false;
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

    final dotColor = job.running
        ? t.a700
        : job.enabled
            ? t.sage
            : job.autoDisabled
                ? t.danger
                : t.n400;

    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.lg),
        border: Border.all(color: t.hair),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                width: 6,
                height: 6,
                decoration:
                    BoxDecoration(color: dotColor, shape: BoxShape.circle),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  job.name,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
                ),
              ),
              _pill(
                t,
                i18n.t('cron:job.action.runNow'),
                enabled: !_busy && !job.running,
                onTap: () => _act(() => api.runNow(job.id)),
              ),
              const SizedBox(width: 6),
              _pill(
                t,
                job.enabled
                    ? i18n.t('cron:job.action.disable')
                    : i18n.t('cron:job.action.enable'),
                enabled: !_busy,
                onTap: () =>
                    _act(() => api.update(job.id, {'enabled': !job.enabled})),
              ),
            ],
          ),
          const SizedBox(height: 6),
          Wrap(
            spacing: 10,
            runSpacing: 4,
            children: [
              Text(describeSchedule(job.schedule, i18n.t),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600)),
              if (job.enabled && job.nextRunAt != null)
                Text(
                  i18n.t('cron:job.nextRun', vars: {
                    'when': formatRelative(job.nextRunAt!, i18n.language)
                  }),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
              if (job.lastRunAt != null)
                Text(
                  i18n.t('cron:job.lastRun', vars: {
                    'when': formatRelative(job.lastRunAt!, i18n.language)
                  }),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
              GestureDetector(
                onTap: () => setState(() => _expanded = !_expanded),
                child: Text(
                  _expanded
                      ? i18n.t('cron:job.hideRuns')
                      : i18n.t('cron:job.showRuns'),
                  style: TextStyle(
                    fontSize: FontSizes.xs,
                    color: t.ink,
                    decoration: TextDecoration.underline,
                  ),
                ),
              ),
            ],
          ),
          if (_expanded)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: CronRunList(jobId: job.id),
            ),
        ],
      ),
    );
  }

  Widget _pill(BossipTokens t, String label,
      {required bool enabled, required VoidCallback onTap}) {
    return Opacity(
      opacity: enabled ? 1 : 0.5,
      child: OutlinedButton(
        onPressed: enabled ? onTap : null,
        style: OutlinedButton.styleFrom(
          side: BorderSide(color: t.hair),
          foregroundColor: t.n800,
          minimumSize: const Size(0, 28),
          padding: const EdgeInsets.symmetric(horizontal: 10),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(Radii.full),
          ),
        ),
        child: Text(label, style: const TextStyle(fontSize: FontSizes.xs)),
      ),
    );
  }
}
