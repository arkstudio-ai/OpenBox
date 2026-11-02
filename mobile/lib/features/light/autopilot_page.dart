import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/light_autopilot.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/widgets/toast.dart';
import 'api/light_api.dart';
import 'state/light_providers.dart';
import 'widgets/autopilot_switch.dart';

/// 托管中的事 (plan §4.2 「我的 → 托管中的事」, home banner): every 省心托管
/// job of this workspace, with pause / resume, open the conversation, cancel.
class LightAutopilotPage extends ConsumerWidget {
  const LightAutopilotPage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final jobs = ref.watch(lightAutopilotsProvider);
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        titleSpacing: 0,
        title: Text(
          i18n.t('light:autopilot.title'),
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w600,
            color: t.ink,
          ),
        ),
      ),
      body: RefreshIndicator(
        onRefresh: () => ref.refresh(lightAutopilotsProvider.future),
        child: jobs.when(
          loading: () =>
              const Center(child: CircularProgressIndicator(strokeWidth: 2)),
          error: (error, _) => ListView(
            padding: const EdgeInsets.all(24),
            children: [
              Text(
                errorText(i18n, error),
                textAlign: TextAlign.center,
                style: TextStyle(color: t.n700),
              ),
            ],
          ),
          data: (items) => ListView(
            key: const Key('light-autopilot-list'),
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
            children: [
              Text(
                i18n.t('light:autopilot.lede'),
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  height: 1.45,
                  color: t.n600,
                ),
              ),
              const SizedBox(height: 14),
              if (items.isEmpty)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 48),
                  child: Column(
                    children: [
                      Icon(Icons.autorenew, size: 40, color: t.n400),
                      const SizedBox(height: 12),
                      Text(
                        i18n.t('light:autopilot.empty'),
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          fontSize: FontSizes.base,
                          height: 1.5,
                          color: t.n700,
                        ),
                      ),
                    ],
                  ),
                )
              else
                for (final job in items) ...[
                  _JobCard(job: job),
                  const SizedBox(height: 10),
                ],
            ],
          ),
        ),
      ),
    );
  }
}

class _JobCard extends ConsumerStatefulWidget {
  const _JobCard({required this.job});

  final LightAutopilot job;

  @override
  ConsumerState<_JobCard> createState() => _JobCardState();
}

class _JobCardState extends ConsumerState<_JobCard> {
  bool _busy = false;

  Future<void> _run(Future<void> Function(LightApi api) action) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await action(ref.read(lightApiProvider));
      ref.invalidate(lightAutopilotsProvider);
    } on DioException catch (error) {
      ref
          .read(toastProvider.notifier)
          .error(errorText(ref.read(i18nProvider), error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _cancel() async {
    final i18n = ref.read(i18nProvider);
    final sure = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(i18n.t('light:autopilot.cancelTitle')),
        content: Text(i18n.t('light:autopilot.cancelBody')),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: Text(i18n.t('light:autopilot.keep')),
          ),
          TextButton(
            key: const Key('light-autopilot-cancel-confirm'),
            onPressed: () => Navigator.of(context).pop(true),
            child: Text(i18n.t('light:autopilot.cancel')),
          ),
        ],
      ),
    );
    if (sure == true) {
      await _run((api) => api.cancelAutopilot(widget.job.id));
    }
  }

  String _status(I18nState i18n) {
    final job = widget.job;
    if (!job.enabled) return i18n.t('light:autopilot.statusPaused');
    if (job.running) return i18n.t('light:autopilot.statusRunning');
    final next = job.nextRunAt;
    if (next == null) return '';
    final day = '${next.month}/${next.day}';
    final time =
        '${next.hour.toString().padLeft(2, '0')}:${next.minute.toString().padLeft(2, '0')}';
    return i18n.t('light:autopilot.statusNext', vars: {'when': '$day $time'});
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final job = widget.job;
    final title = [
      i18n.t('light:autopilot.task.${job.task}'),
      if (job.shopName.isNotEmpty) job.shopName,
    ].join(' · ');
    final last = job.lastStatus == null
        ? ''
        : i18n.t('light:autopilot.last.${job.lastStatus}');
    return Container(
      key: Key('light-autopilot-job-${job.id}'),
      padding: const EdgeInsets.fromLTRB(16, 14, 12, 8),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.xl),
        border: Border.all(color: t.hair),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(
                job.enabled ? Icons.autorenew : Icons.pause_circle_outline,
                size: 18,
                color: job.enabled ? t.a800 : t.n500,
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: FontSizes.lg,
                    fontWeight: FontWeight.w700,
                    color: t.ink,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 6),
          Text(
            [
              lightAutopilotWhen(i18n, job.repeat, job.time),
              i18n.t('light:autopilot.mode.${job.mode}'),
              i18n.t(
                'light:autopilot.capLine',
                vars: {'value': job.creditsCap},
              ),
            ].join(' · '),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
          ),
          const SizedBox(height: 2),
          Text(
            [_status(i18n), last].where((s) => s.isNotEmpty).join(' · '),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
          ),
          Row(
            children: [
              TextButton(
                key: Key('light-autopilot-toggle-${job.id}'),
                onPressed: _busy
                    ? null
                    : () => _run(
                        (api) => api.setAutopilotEnabled(job.id, !job.enabled),
                      ),
                child: Text(
                  i18n.t(
                    job.enabled
                        ? 'light:autopilot.pause'
                        : 'light:autopilot.resume',
                  ),
                ),
              ),
              if (job.sessionId != null)
                TextButton(
                  onPressed: () =>
                      context.push(Paths.lightChat(job.sessionId!)),
                  child: Text(i18n.t('light:autopilot.open')),
                ),
              const Spacer(),
              TextButton(
                key: Key('light-autopilot-cancel-${job.id}'),
                onPressed: _busy ? null : _cancel,
                style: TextButton.styleFrom(foregroundColor: t.n600),
                child: Text(i18n.t('light:autopilot.cancel')),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
