import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/utils/format.dart';
import 'api/cron_api.dart';
import 'widgets/chat_create_dialog.dart';
import 'widgets/cron_job_card.dart';
import 'widgets/cron_job_form.dart';

/// Scheduled-tasks page (web `CronRoute` + `CronPage`): create menu
/// (manual / via chat), pause/resume all, job cards, scheduler status line.
class CronScreen extends ConsumerWidget {
  const CronScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final jobs = ref.watch(cronJobsProvider);
    final list = jobs.valueOrNull ?? const [];
    final anyEnabled = list.any((j) => j.enabled);

    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(i18n.t('cron:page.title'),
                style: TextStyle(
                    fontSize: FontSizes.lg,
                    fontWeight: FontWeight.w500,
                    color: t.ink)),
            Text(i18n.t('cron:page.subtitle'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600)),
          ],
        ),
        titleSpacing: 0,
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(cronJobsProvider);
          ref.invalidate(cronStatusProvider);
        },
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
          children: [
            Wrap(
              spacing: 8,
              runSpacing: 8,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                FilledButton(
                  onPressed: () => _showCreateMenu(context, ref),
                  style: FilledButton.styleFrom(
                    backgroundColor: t.ink,
                    foregroundColor: t.bg,
                    minimumSize: const Size(0, 36),
                    padding: const EdgeInsets.symmetric(horizontal: 16),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(Radii.full),
                    ),
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Text(i18n.t('cron:page.create'),
                          style: const TextStyle(fontSize: FontSizes.sm)),
                      const SizedBox(width: 5),
                      const Icon(Icons.expand_more, size: 15),
                    ],
                  ),
                ),
                if (list.isNotEmpty)
                  OutlinedButton(
                    onPressed: () async {
                      final api = ref.read(cronApiProvider);
                      if (anyEnabled) {
                        await api.pauseAll();
                      } else {
                        await api.resumeAll();
                      }
                      ref.invalidate(cronJobsProvider);
                      ref.invalidate(cronStatusProvider);
                    },
                    style: OutlinedButton.styleFrom(
                      side: BorderSide(color: t.hair),
                      foregroundColor: t.n800,
                      minimumSize: const Size(0, 32),
                      padding: const EdgeInsets.symmetric(horizontal: 14),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(Radii.full),
                      ),
                    ),
                    child: Text(
                      anyEnabled
                          ? i18n.t('cron:page.pauseAll')
                          : i18n.t('cron:page.resumeAll'),
                      style: const TextStyle(fontSize: FontSizes.xs),
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 14),
            ...jobs.when(
              loading: () => [
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 24),
                  child: Text(i18n.t('cron:page.loading'),
                      textAlign: TextAlign.center,
                      style:
                          TextStyle(fontSize: FontSizes.sm, color: t.n600)),
                ),
              ],
              error: (_, _) => [
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 24),
                  child: Text(i18n.t('cron:page.loadFailed'),
                      style: TextStyle(
                          fontSize: FontSizes.sm, color: t.danger)),
                ),
              ],
              data: (data) => data.isEmpty
                  ? [
                      Container(
                        padding: const EdgeInsets.fromLTRB(16, 20, 16, 20),
                        decoration: BoxDecoration(
                          color: t.card,
                          borderRadius: BorderRadius.circular(Radii.lg),
                          border: Border.all(color: t.hair),
                        ),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(i18n.t('cron:page.empty.title'),
                                style: TextStyle(
                                    fontSize: FontSizes.base,
                                    color: t.ink)),
                            const SizedBox(height: 4),
                            Text(i18n.t('cron:page.empty.body'),
                                style: TextStyle(
                                    fontSize: FontSizes.sm,
                                    color: t.n600,
                                    height: 1.6)),
                          ],
                        ),
                      ),
                    ]
                  : [
                      for (final job in data)
                        CronJobCard(
                          job: job,
                          onEdit: (j) => showCronJobForm(context, job: j),
                        ),
                    ],
            ),
            const SizedBox(height: 8),
            _StatusLine(),
          ],
        ),
      ),
    );
  }

  void _showCreateMenu(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.read(i18nProvider);
    showModalBottomSheet<void>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: Icon(Icons.edit_calendar_outlined,
                  size: 20, color: t.n700),
              title: Text(i18n.t('cron:page.createManual'),
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
              onTap: () {
                Navigator.pop(sheetContext);
                showCronJobForm(context);
              },
            ),
            ListTile(
              leading: Icon(Icons.chat_bubble_outline,
                  size: 20, color: t.n700),
              title: Text(i18n.t('cron:page.createChat'),
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
              onTap: () {
                Navigator.pop(sheetContext);
                showCronChatCreateDialog(context);
              },
            ),
          ],
        ),
      ),
    );
  }
}

/// Scheduler liveness footer: only alarming when actually unhealthy
/// (web `StatusLine`).
class _StatusLine extends ConsumerWidget {
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final status = ref.watch(cronStatusProvider).valueOrNull;
    if (status == null) return const SizedBox.shrink();
    return Wrap(
      spacing: 12,
      runSpacing: 4,
      children: [
        Text(
          status.healthy
              ? i18n.t('cron:status.healthy')
              : i18n.t('cron:status.unhealthy'),
          style: TextStyle(
            fontSize: FontSizes.xs,
            color: status.healthy ? t.n500 : t.danger,
          ),
        ),
        if (status.lastTickAt != null)
          Text(
            i18n.t('cron:status.lastTick', vars: {
              'when': formatRelative(status.lastTickAt!, i18n.language)
            }),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
          ),
        Text(
          i18n.t('cron:status.enabledCount', count: status.enabledJobs),
          style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
        ),
        if (status.runningJobs > 0)
          Text(
            i18n.t('cron:status.runningCount', count: status.runningJobs),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
          ),
      ],
    );
  }
}
