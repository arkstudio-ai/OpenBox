import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/cron.dart';
import '../../../shared/utils/format.dart';
import '../api/cron_api.dart';

/// Aggregate view of a project's jobs for the pill (web `summarize`).
class CronPillSummary {
  const CronPillSummary({
    required this.running,
    required this.failedCount,
    required this.nextRun,
    required this.lastRun,
    required this.autoDisabled,
    required this.anyEnabled,
  });

  final bool running;
  final int failedCount;
  final DateTime? nextRun;
  final DateTime? lastRun;
  final bool autoDisabled;
  final bool anyEnabled;
}

CronPillSummary summarizeCronJobs(List<CronJob> jobs) {
  final nextRuns = jobs
      .where((j) => j.enabled && j.nextRunAt != null)
      .map((j) => j.nextRunAt!)
      .toList()
    ..sort();
  final lastRuns =
      jobs.where((j) => j.lastRunAt != null).map((j) => j.lastRunAt!).toList()
        ..sort();
  return CronPillSummary(
    running: jobs.any((j) => j.running),
    failedCount: jobs.where((j) => j.lastStatus == 'error').length,
    nextRun: nextRuns.firstOrNull,
    lastRun: lastRuns.lastOrNull,
    autoDisabled: jobs.any((j) => j.autoDisabled),
    anyEnabled: jobs.any((j) => j.enabled),
  );
}

/// Topbar pill (web `CronStatusPill`): last-run state dot + time to the next
/// run. Hidden when the session's project has no scheduled tasks. Tapping
/// opens the workbench cron tab.
class CronStatusPill extends ConsumerWidget {
  const CronStatusPill({
    super.key,
    required this.projectId,
    required this.onOpen,
  });

  final String? projectId;
  final VoidCallback onOpen;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    if (projectId == null) return const SizedBox.shrink();
    final jobs = ref.watch(cronJobsProvider).valueOrNull ?? const <CronJob>[];
    final mine = jobs.where((j) => j.projectId == projectId).toList();
    if (mine.isEmpty) return const SizedBox.shrink();

    final s = summarizeCronJobs(mine);
    final dotColor = s.running
        ? t.a700
        : (s.failedCount > 0 || s.autoDisabled)
            ? t.danger
            : s.anyEnabled
                ? t.sage
                : t.n400;

    final label = s.running
        ? i18n.t('cron:pill.running')
        : s.nextRun != null
            ? formatRelative(s.nextRun!, i18n.language)
            : i18n.t('cron:pill.paused');

    return Semantics(
      label: i18n.t('cron:pill.aria'),
      button: true,
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.full),
        onTap: onOpen,
        child: Container(
          height: 30,
          padding: const EdgeInsets.symmetric(horizontal: 10),
          decoration: BoxDecoration(
            border: Border.all(color: t.hair),
            borderRadius: BorderRadius.circular(Radii.full),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 6,
                height: 6,
                decoration:
                    BoxDecoration(color: dotColor, shape: BoxShape.circle),
              ),
              const SizedBox(width: 6),
              ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 96),
                child: Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
                ),
              ),
              if (s.failedCount > 0) ...[
                const SizedBox(width: 5),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 6),
                  decoration: BoxDecoration(
                    color: t.dangerSoft,
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                  child: Text(
                    '${s.failedCount}',
                    style: TextStyle(
                        fontSize: FontSizes.xs2, color: t.dangerInk),
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
