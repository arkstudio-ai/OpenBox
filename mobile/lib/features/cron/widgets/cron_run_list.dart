import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/cron.dart';
import '../../../shared/utils/format.dart';
import '../api/cron_api.dart';
import '../utils/schedule.dart';

/// Execution history for one job, one row per run (web `CronRunList`).
/// Tapping a run that left a transcript opens it; a failed run shows why.
class CronRunList extends ConsumerWidget {
  const CronRunList({super.key, required this.jobId, required this.onOpen});

  final String jobId;

  /// Called with the run's transcript session id.
  final void Function(String sessionId) onOpen;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final runs = ref.watch(cronRunsProvider(jobId));
    return runs.when(
      loading: () => Padding(
        padding: const EdgeInsets.symmetric(vertical: 10),
        child: Text(
          i18n.t('cron:run.loading'),
          style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
        ),
      ),
      error: (_, _) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 10),
        child: Text(
          i18n.t('cron:run.loadFailed'),
          style: TextStyle(fontSize: FontSizes.xs, color: t.danger),
        ),
      ),
      data: (list) {
        if (list.isEmpty) {
          return Padding(
            padding: const EdgeInsets.symmetric(vertical: 10),
            child: Text(
              i18n.t('cron:run.empty'),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
          );
        }
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            for (final run in list) _RunRow(run: run, onOpen: onOpen),
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                i18n.t('cron:run.retentionHint'),
                style: TextStyle(fontSize: FontSizes.xs2, color: t.n500),
              ),
            ),
          ],
        );
      },
    );
  }
}

class _RunRow extends ConsumerWidget {
  const _RunRow({required this.run, required this.onOpen});

  final CronRun run;
  final void Function(String sessionId) onOpen;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final silent = run.status == 'ok' && isSilentResult(run.summaryText);
    final sessionId = run.tempSessionId;

    final (chipBg, chipFg) = switch (run.status) {
      'ok' => (t.n300, t.n800),
      'error' => (t.dangerSoft, t.danger),
      _ => (t.hairSoft, t.n600),
    };

    return InkWell(
      borderRadius: BorderRadius.circular(Radii.lg),
      onTap: sessionId == null ? null : () => onOpen(sessionId),
      child: Container(
        padding: const EdgeInsets.fromLTRB(10, 10, 6, 10),
        decoration: BoxDecoration(
          border: Border(top: BorderSide(color: t.hairSoft)),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Wrap(
                    spacing: 8,
                    runSpacing: 4,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      Container(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 8,
                          vertical: 2,
                        ),
                        decoration: BoxDecoration(
                          color: chipBg,
                          borderRadius: BorderRadius.circular(Radii.full),
                        ),
                        child: Text(
                          i18n.t(
                            runStatusKeys[run.status] ??
                                runStatusKeys['skipped']!,
                          ),
                          style: TextStyle(
                            fontSize: FontSizes.xs,
                            color: chipFg,
                          ),
                        ),
                      ),
                      if (silent)
                        Container(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 8,
                            vertical: 2,
                          ),
                          decoration: BoxDecoration(
                            color: t.hairSoft,
                            borderRadius: BorderRadius.circular(Radii.full),
                          ),
                          child: Text(
                            i18n.t('cron:run.silent'),
                            style: TextStyle(
                              fontSize: FontSizes.xs,
                              color: t.n600,
                            ),
                          ),
                        ),
                      if (run.startedAt != null)
                        Text(
                          formatRelative(run.startedAt!, i18n.language),
                          style: TextStyle(
                            fontSize: FontSizes.xs,
                            color: t.n600,
                          ),
                        ),
                      Text(
                        formatDuration(run.durationMs / 1000),
                        style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                      ),
                      if (run.totalTokens > 0)
                        Text(
                          i18n.t(
                            'cron:run.tokens',
                            vars: {'formatted': formatTokens(run.totalTokens)},
                          ),
                          style: TextStyle(
                            fontSize: FontSizes.xs,
                            color: t.n600,
                          ),
                        ),
                    ],
                  ),
                  if (run.status == 'error')
                    Padding(
                      padding: const EdgeInsets.only(top: 4),
                      child: Text(
                        [
                          i18n.t('cron:run.failed'),
                          if ((run.errorMessage ?? '').isNotEmpty)
                            run.errorMessage!,
                        ].join(' '),
                        maxLines: 3,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontSize: FontSizes.xs,
                          color: t.danger,
                          height: 1.5,
                        ),
                      ),
                    )
                  else if (!silent &&
                      run.summaryText != null &&
                      run.summaryText!.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(top: 4),
                      child: Text(
                        run.summaryText!,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontSize: FontSizes.xs,
                          color: t.n700,
                          height: 1.5,
                        ),
                      ),
                    ),
                ],
              ),
            ),
            if (sessionId != null)
              Padding(
                padding: const EdgeInsets.only(left: 6),
                child: Icon(Icons.chevron_right, size: 18, color: t.n500),
              ),
          ],
        ),
      ),
    );
  }
}
