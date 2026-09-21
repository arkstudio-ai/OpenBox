import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/team.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/task_card_frame.dart';
import '../../../shared/widgets/toast.dart';
import '../api/teams_api.dart';
import '../state/team_providers.dart';

class TeamControls extends ConsumerStatefulWidget {
  const TeamControls({
    super.key,
    required this.scope,
    required this.run,
    this.onDetails,
  });
  final TeamScope scope;
  final TeamRun run;
  final VoidCallback? onDetails;
  @override
  ConsumerState<TeamControls> createState() => _TeamControlsState();
}

class _TeamControlsState extends ConsumerState<TeamControls> {
  bool _busy = false;
  Future<void> _control(String action) async {
    if (_busy) return;
    setState(() => _busy = true);
    final api = ref.read(teamsApiProvider);
    final toast = ref.read(toastProvider.notifier);
    final i18n = ref.read(i18nProvider);
    final controller = ref.read(
      teamRunProvider((scope: widget.scope, runId: widget.run.id)).notifier,
    );
    final key =
        'mobile-${DateTime.now().microsecondsSinceEpoch}-${Random.secure().nextInt(1 << 32)}';
    try {
      await api.control(widget.scope, widget.run, action, key);
    } catch (error) {
      toast.error(errorText(i18n, error));
    } finally {
      await controller.refresh();
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final run = widget.run;
    return Wrap(
      spacing: 4,
      children: [
        if (widget.onDetails != null)
          TextButton(
            onPressed: widget.onDetails,
            child: Text(i18n.t('teams:details')),
          ),
        if (run.state == 'running' || run.state == 'provisioning')
          TextButton(
            onPressed: _busy ? null : () => _control('pause'),
            child: Text(i18n.t('teams:pause')),
          ),
        if (run.state == 'paused')
          TextButton(
            onPressed: _busy ? null : () => _control('resume'),
            child: Text(i18n.t('teams:resume')),
          ),
        if (!run.terminal &&
            !const ['canceling', 'completing'].contains(run.state))
          TextButton(
            onPressed: _busy ? null : () => _control('cancel'),
            child: Text(i18n.t('teams:cancelRun')),
          ),
        if (run.finalSummary.isNotEmpty)
          TextButton(
            onPressed: () async {
              final toast = ref.read(toastProvider.notifier);
              await Clipboard.setData(ClipboardData(text: run.finalSummary));
              toast.success(i18n.t('teams:finalCopied'));
            },
            child: Text(i18n.t('teams:copyFinal')),
          ),
      ],
    );
  }
}

class TeamProgressCard extends ConsumerWidget {
  const TeamProgressCard({
    super.key,
    required this.scope,
    required this.runId,
    required this.onDetails,
  });
  final TeamScope scope;
  final String runId;
  final VoidCallback onDetails;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final value = ref.watch(teamRunProvider((scope: scope, runId: runId)));
    final snapshot = value.valueOrNull;
    final i18n = ref.watch(i18nProvider);
    if (snapshot == null) {
      return value.hasError
          ? TextButton(
              onPressed: () =>
                  ref.invalidate(teamRunProvider((scope: scope, runId: runId))),
              child: Text(i18n.t('common:action.retry')),
            )
          : const LinearProgressIndicator();
    }
    final run = snapshot.run;
    return TaskCardFrame(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Icon(Icons.groups_outlined, size: 18, color: context.tokens.n600),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  run.title,
                  style: const TextStyle(fontWeight: FontWeight.w500),
                ),
              ),
              const SizedBox(width: 8),
              Text(i18n.t('teams:state.${run.state}')),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            i18n.t(
              'teams:taskCount',
              vars: {
                'done': snapshot.completedTaskCount,
                'total': snapshot.taskCount,
              },
            ),
          ),
          const SizedBox(height: 8),
          LinearProgressIndicator(
            value: snapshot.taskCount == 0
                ? 0
                : (snapshot.completedTaskCount / snapshot.taskCount).clamp(
                    0,
                    1,
                  ),
            color: context.tokens.accent,
            backgroundColor: context.tokens.hair,
          ),
          if (run.pauseReason != null)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(i18n.t('teams:reason.${run.pauseReason}')),
            ),
          if (run.failureReason?.isNotEmpty == true) Text(run.failureReason!),
          if (value.hasError)
            Text(
              errorText(i18n, value.error!),
              style: TextStyle(color: context.tokens.danger),
            ),
          TeamControls(scope: scope, run: run, onDetails: onDetails),
        ],
      ),
    );
  }
}
