import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/router/paths.dart';
import 'api/cron_api.dart';
import 'widgets/cron_job_card.dart';
import 'widgets/cron_job_form.dart';
import 'widgets/cron_run_list.dart';

/// One scheduled task (web `CronJobPage`): settings and actions up top, the
/// run history below; a run that left a transcript opens it as a chat. On a
/// phone the transcript is its own screen rather than a pane beside the list.
class CronJobScreen extends ConsumerWidget {
  const CronJobScreen({super.key, required this.jobId});

  final String jobId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final jobs = ref.watch(cronJobsProvider);
    final job = jobs.valueOrNull?.where((j) => j.id == jobId).firstOrNull;

    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        title: Text(
          job?.name ?? i18n.t('cron:page.title'),
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
        titleSpacing: 0,
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(cronJobsProvider);
          ref.invalidate(cronRunsProvider(jobId));
        },
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
          children: [
            if (job == null)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 24),
                child: Text(
                  jobs.isLoading && !jobs.hasValue
                      ? i18n.t('cron:page.loading')
                      : jobs.hasError
                      ? i18n.t('cron:page.loadFailed')
                      : i18n.t('cron:detail.notFound'),
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    color: jobs.hasError ? t.danger : t.n600,
                  ),
                ),
              )
            else ...[
              Container(
                padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
                decoration: BoxDecoration(
                  color: t.card,
                  borderRadius: BorderRadius.circular(Radii.lg),
                  border: Border.all(color: t.hair),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    CronStateDot(job: job),
                    const SizedBox(height: 6),
                    Text(
                      job.taskPrompt,
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
                          style: TextStyle(
                            fontSize: FontSizes.xs,
                            color: t.danger,
                          ),
                        ),
                      ),
                    const SizedBox(height: 10),
                    CronJobActions(
                      job: job,
                      onEdit: (j) => showCronJobForm(context, job: j),
                      onDeleted: () {
                        if (context.canPop()) context.pop();
                      },
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 16),
              Text(
                i18n.t('cron:detail.runs'),
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  fontWeight: FontWeight.w500,
                  color: t.n600,
                ),
              ),
              const SizedBox(height: 4),
              CronRunList(
                jobId: jobId,
                onOpen: (sessionId) => context.push(Paths.chat(sessionId)),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
