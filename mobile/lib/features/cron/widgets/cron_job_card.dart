import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/cron.dart';
import '../../../shared/utils/format.dart';
import '../api/cron_api.dart';
import '../utils/schedule.dart';
import 'cron_run_list.dart';

/// One scheduled job (web `CronJobCard`): name + state dot, prompt excerpt,
/// schedule/next/last/stats meta, action pills, expandable run history.
class CronJobCard extends ConsumerStatefulWidget {
  const CronJobCard({super.key, required this.job, required this.onEdit});

  final CronJob job;
  final void Function(CronJob job) onEdit;

  @override
  ConsumerState<CronJobCard> createState() => _CronJobCardState();
}

class _CronJobCardState extends ConsumerState<CronJobCard> {
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

    final (dotColor, stateLabel) = job.running
        ? (t.a700, i18n.t('cron:job.state.running'))
        : job.enabled
            ? (t.sage, i18n.t('cron:job.state.enabled'))
            : job.autoDisabled
                ? (t.danger, i18n.t('cron:job.state.autoDisabled'))
                : (t.n400, i18n.t('cron:job.state.disabled'));

    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
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
              Expanded(
                child: Text(
                  job.name,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                ),
              ),
              const SizedBox(width: 8),
              Container(
                width: 6,
                height: 6,
                decoration:
                    BoxDecoration(color: dotColor, shape: BoxShape.circle),
              ),
              const SizedBox(width: 6),
              Text(stateLabel,
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600)),
            ],
          ),
          const SizedBox(height: 4),
          Text(
            job.taskPrompt,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
                fontSize: FontSizes.sm, color: t.n700, height: 1.5),
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 12,
            runSpacing: 4,
            children: [
              _meta(t, describeSchedule(job.schedule, i18n.t)),
              if (job.enabled && job.nextRunAt != null)
                _meta(
                    t,
                    i18n.t('cron:job.nextRun', vars: {
                      'when': formatRelative(job.nextRunAt!, i18n.language)
                    })),
              if (job.lastRunAt != null)
                _meta(
                    t,
                    i18n.t('cron:job.lastRun', vars: {
                      'when': formatRelative(job.lastRunAt!, i18n.language)
                    })),
              _meta(
                  t,
                  i18n.t('cron:job.stats', vars: {
                    'total': job.totalRuns,
                    'ok': job.totalSuccesses,
                    'failed': job.totalFailures,
                  })),
            ],
          ),
          if ((job.lastError ?? '').isNotEmpty && !job.enabled)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                i18n.t('cron:job.lastError'),
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(fontSize: FontSizes.xs, color: t.danger),
              ),
            ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            crossAxisAlignment: WrapCrossAlignment.center,
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
              GestureDetector(
                onTap: () => setState(() => _expanded = !_expanded),
                child: Padding(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 6, vertical: 6),
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
              ),
            ],
          ),
          if (_expanded)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: CronRunList(jobId: job.id),
            ),
        ],
      ),
    );
  }

  Widget _meta(BossipTokens t, String text) => Text(
        text,
        style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
      );

  Widget _pill(BossipTokens t, String label,
      {required bool enabled, required VoidCallback onTap, bool danger = false}) {
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
        title: Text(i18n.t('cron:job.deleteConfirm.title'),
            style: const TextStyle(fontSize: FontSizes.lg)),
        content: Text(
          i18n.t('cron:job.deleteConfirm.body',
              vars: {'name': widget.job.name}),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: Text(i18n.t('cron:form.cancel')),
          ),
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, true),
            child: Text(i18n.t('cron:job.action.delete'),
                style: TextStyle(color: t.danger)),
          ),
        ],
      ),
    );
    if (confirmed == true) {
      await _act(() => ref.read(cronApiProvider).delete(widget.job.id));
    }
  }
}
