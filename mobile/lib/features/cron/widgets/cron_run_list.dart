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

/// Execution history for one job, loaded only while expanded
/// (web `CronRunList`).
class CronRunList extends ConsumerWidget {
  const CronRunList({super.key, required this.jobId});

  final String jobId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final runs = ref.watch(cronRunsProvider(jobId));
    return runs.when(
      loading: () => Padding(
        padding: const EdgeInsets.symmetric(vertical: 10),
        child: Text(i18n.t('cron:run.loading'),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n600)),
      ),
      error: (_, _) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 10),
        child: Text(i18n.t('cron:run.loadFailed'),
            style: TextStyle(fontSize: FontSizes.xs, color: t.danger)),
      ),
      data: (list) {
        if (list.isEmpty) {
          return Padding(
            padding: const EdgeInsets.symmetric(vertical: 10),
            child: Text(i18n.t('cron:run.empty'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600)),
          );
        }
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            for (final run in list) _RunRow(run: run),
            Padding(
              padding: const EdgeInsets.only(top: 4),
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
  const _RunRow({required this.run});

  final CronRun run;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final silent = run.status == 'ok' && isSilentResult(run.summaryText);

    final (chipBg, chipFg) = switch (run.status) {
      'ok' => (t.n300, t.n800),
      'error' => (t.dangerSoft, t.danger),
      _ => (t.hairSoft, t.n600),
    };

    return Container(
      padding: const EdgeInsets.symmetric(vertical: 9),
      decoration: BoxDecoration(
        border: Border(top: BorderSide(color: t.hairSoft)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Wrap(
            spacing: 8,
            runSpacing: 4,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              Container(
                padding:
                    const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                decoration: BoxDecoration(
                  color: chipBg,
                  borderRadius: BorderRadius.circular(Radii.full),
                ),
                child: Text(
                  i18n.t(runStatusKeys[run.status] ?? runStatusKeys['skipped']!),
                  style: TextStyle(fontSize: FontSizes.xs, color: chipFg),
                ),
              ),
              if (silent)
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                  decoration: BoxDecoration(
                    color: t.hairSoft,
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                  child: Text(i18n.t('cron:run.silent'),
                      style:
                          TextStyle(fontSize: FontSizes.xs, color: t.n600)),
                ),
              if (run.startedAt != null)
                Text(
                  formatRelative(run.startedAt!, i18n.language),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
              Text(
                formatDuration(run.durationMs / 1000),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
              if (run.totalTokens > 0)
                Text(
                  i18n.t('cron:run.tokens',
                      vars: {'formatted': formatTokens(run.totalTokens)}),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
              if (run.tempSessionId != null)
                GestureDetector(
                  onTap: () => context.push(Paths.chat(run.tempSessionId!)),
                  child: Text(
                    i18n.t('cron:run.transcript'),
                    style: TextStyle(
                      fontSize: FontSizes.xs,
                      color: t.ink,
                      decoration: TextDecoration.underline,
                    ),
                  ),
                ),
            ],
          ),
          if (run.status == 'error')
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(i18n.t('cron:run.failed'),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.danger)),
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
                    fontSize: FontSizes.xs, color: t.n700, height: 1.5),
              ),
            ),
        ],
      ),
    );
  }
}
