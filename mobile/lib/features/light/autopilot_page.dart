import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/cron.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/widgets/toast.dart';
import '../chat/api/assistant_api.dart';
import '../cron/api/cron_api.dart';
import '../cron/utils/schedule.dart';
import '../cron/widgets/cron_job_form.dart';
import 'api/light_api.dart';
import 'state/light_providers.dart';

class LightAutopilotPage extends ConsumerStatefulWidget {
  const LightAutopilotPage({super.key});

  @override
  ConsumerState<LightAutopilotPage> createState() => _LightAutopilotPageState();
}

class _LightAutopilotPageState extends ConsumerState<LightAutopilotPage> {
  final _pending = <String>{};

  Future<void> _refresh() async {
    try {
      ref.invalidate(cronJobsProvider);
      await ref.read(cronJobsProvider.future);
    } catch (_) {
      // The list renders the failure and its retry action.
    }
  }

  Future<void> _change(CronJob job, Future<void> Function() action) async {
    if (!_pending.add(job.id)) return;
    setState(() {});
    try {
      await action();
    } catch (error) {
      if (!mounted) return;
      ref
          .read(toastProvider.notifier)
          .error(errorText(ref.read(i18nProvider), error));
    } finally {
      if (mounted) {
        ref.invalidate(cronStatusProvider);
        if (job.isLightAutopilot) ref.invalidate(lightAutopilotsProvider);
        await _refresh();
        if (mounted) setState(() => _pending.remove(job.id));
      }
    }
  }

  Future<void> _toggle(CronJob job, bool enabled) => _change(job, () async {
    if (job.managedByAssistant) {
      final scope = ref.read(assistantScopeProvider);
      if (scope == null) return;
      await ref
          .read(assistantApiProvider(scope))
          .setScheduleEnabled(job.id, job.revision, enabled);
    } else if (job.isLightAutopilot) {
      await ref.read(lightApiProvider).setAutopilotEnabled(job.id, enabled);
    } else {
      await ref.read(cronApiProvider).update(job.id, {'enabled': enabled});
    }
  });

  Future<void> _open(CronJob job) async {
    if (job.managedByAssistant) {
      context.go(Paths.lightAssistant);
      return;
    }
    if (!job.isLightAutopilot) {
      await showCronJobForm(context, job: job);
      return;
    }
    final i18n = ref.read(i18nProvider);
    final action = await showModalBottomSheet<String>(
      context: context,
      builder: (sheet) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (job.sessionId != null)
              ListTile(
                leading: const Icon(Icons.chat_bubble_outline),
                title: Text(i18n.t('light:autopilot.open')),
                onTap: () => Navigator.pop(sheet, 'open'),
              ),
            ListTile(
              key: Key('light-autopilot-cancel-${job.id}'),
              leading: const Icon(Icons.event_busy_outlined),
              title: Text(i18n.t('light:autopilot.cancel')),
              onTap: () => Navigator.pop(sheet, 'cancel'),
            ),
          ],
        ),
      ),
    );
    if (!mounted) return;
    if (action == 'open' && job.sessionId != null) {
      await context.push(Paths.lightChat(job.sessionId!));
    } else if (action == 'cancel') {
      final confirmed = await showDialog<bool>(
        context: context,
        builder: (dialog) => AlertDialog(
          title: Text(i18n.t('light:autopilot.cancelTitle')),
          content: Text(i18n.t('light:autopilot.cancelBody')),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialog, false),
              child: Text(i18n.t('light:autopilot.keep')),
            ),
            TextButton(
              key: const Key('light-autopilot-cancel-confirm'),
              onPressed: () => Navigator.pop(dialog, true),
              child: Text(i18n.t('light:autopilot.cancel')),
            ),
          ],
        ),
      );
      if (mounted && confirmed == true) {
        await _change(
          job,
          () => ref.read(lightApiProvider).cancelAutopilot(job.id),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final jobs = ref.watch(cronJobsProvider);
    final scope = ref.watch(assistantScopeProvider);
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        titleSpacing: 0,
        title: Text(
          i18n.t('light:autopilot.title'),
          style: TextStyle(
            fontSize: FontSizes.xl2,
            fontWeight: FontWeight.w800,
            color: t.ink,
          ),
        ),
      ),
      body: RefreshIndicator(
        onRefresh: _refresh,
        child: ListView(
          key: const Key('light-autopilot-list'),
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
          physics: const AlwaysScrollableScrollPhysics(),
          children: jobs.when(
            loading: () => const [
              Padding(
                padding: EdgeInsets.only(top: 32),
                child: Center(child: CircularProgressIndicator(strokeWidth: 2)),
              ),
            ],
            error: (error, _) => [
              Text(
                errorText(i18n, error),
                textAlign: TextAlign.center,
                style: TextStyle(color: t.n700),
              ),
              TextButton(
                onPressed: _refresh,
                child: Text(i18n.t('common:action.retry')),
              ),
            ],
            data: (items) {
              if (items.isEmpty) {
                return [
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 44),
                    child: Column(
                      children: [
                        Icon(Icons.schedule_outlined, size: 35, color: t.n700),
                        const SizedBox(height: 12),
                        Text(
                          i18n.t('light:autopilot.noJobs'),
                          textAlign: TextAlign.center,
                          style: TextStyle(color: t.n700),
                        ),
                      ],
                    ),
                  ),
                ];
              }
              return [
                for (final (enabled, group) in [
                  (true, items.where((job) => job.enabled).toList()),
                  (false, items.where((job) => !job.enabled).toList()),
                ])
                  if (group.isNotEmpty) ...[
                    Text(
                      i18n.t(
                        enabled
                            ? 'light:autopilot.groupEnabled'
                            : 'light:autopilot.statusPaused',
                      ),
                      style: TextStyle(
                        fontSize: FontSizes.xl,
                        fontWeight: FontWeight.w800,
                        color: t.ink,
                      ),
                    ),
                    const SizedBox(height: 10),
                    Material(
                      color: t.hairSoft,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(Radii.xl),
                        side: BorderSide(color: t.hair),
                      ),
                      clipBehavior: Clip.antiAlias,
                      child: Column(
                        children: [
                          for (final (index, job) in group.indexed) ...[
                            if (index > 0)
                              Divider(
                                height: 1,
                                indent: 14,
                                endIndent: 14,
                                color: t.hair,
                              ),
                            ListTile(
                              key: Key('light-autopilot-job-${job.id}'),
                              contentPadding: const EdgeInsets.fromLTRB(
                                16,
                                4,
                                12,
                                4,
                              ),
                              onTap: _pending.contains(job.id)
                                  ? null
                                  : () => _open(job),
                              title: Text(
                                job.name,
                                style: TextStyle(
                                  fontWeight: FontWeight.w800,
                                  fontSize: FontSizes.lg,
                                  color: t.ink,
                                ),
                              ),
                              subtitle: Text(
                                [
                                  describeSchedule(job.schedule, i18n.t),
                                  if (job.running)
                                    i18n.t('light:autopilot.statusRunning'),
                                  if (job.autoDisabled)
                                    i18n.t('cron:job.state.autoDisabled'),
                                ].join(' · '),
                                style: TextStyle(
                                  color: t.n700,
                                  fontSize: FontSizes.sm,
                                ),
                              ),
                              trailing: Semantics(
                                label: job.name,
                                child: Switch.adaptive(
                                  key: Key('light-schedule-switch-${job.id}'),
                                  value: job.enabled,
                                  activeThumbColor: t.card,
                                  activeTrackColor: t.a700,
                                  onChanged:
                                      _pending.contains(job.id) ||
                                          (job.managedByAssistant &&
                                              scope == null)
                                      ? null
                                      : (enabled) => _toggle(job, enabled),
                                ),
                              ),
                            ),
                          ],
                        ],
                      ),
                    ),
                    const SizedBox(height: 22),
                  ],
              ];
            },
          ),
        ),
      ),
      bottomNavigationBar: SafeArea(
        minimum: const EdgeInsets.fromLTRB(16, 8, 16, 16),
        child: FilledButton(
          key: const Key('light-autopilot-create'),
          onPressed: () => showCronJobForm(context),
          style: FilledButton.styleFrom(
            backgroundColor: t.ink,
            foregroundColor: t.bg,
            minimumSize: const Size.fromHeight(48),
            textStyle: const TextStyle(
              fontSize: FontSizes.lg,
              fontWeight: FontWeight.w700,
            ),
          ),
          child: Text(i18n.t('light:autopilot.create')),
        ),
      ),
    );
  }
}
