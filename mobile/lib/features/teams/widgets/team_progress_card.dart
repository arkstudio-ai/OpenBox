import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/team.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/fold.dart';
import '../../../shared/widgets/task_card_frame.dart';
import '../../../shared/widgets/toast.dart';
import '../api/teams_api.dart';
import '../state/team_providers.dart';
import 'team_bits.dart';

/// Pause / resume / cancel / copy for one run (web `TeamControls`). The
/// actions are links, and a lost response is reconciled from the durable
/// snapshot rather than from what the tap assumed.
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
    // Mid-transition a run has not reached its safe boundary yet; offering the
    // same action again would only queue a duplicate.
    final transitional = const [
      'pausing',
      'canceling',
      'completing',
      'provisioning',
    ].contains(run.state);
    return Wrap(
      spacing: 16,
      runSpacing: 2,
      crossAxisAlignment: WrapCrossAlignment.center,
      children: [
        if (widget.onDetails != null)
          TeamActionLink(
            label: i18n.t('teams:details'),
            onTap: widget.onDetails,
          ),
        if (!run.terminal && !transitional)
          TeamActionLink(
            label: i18n.t(
              run.state == 'paused' ? 'teams:resume' : 'teams:pause',
            ),
            onTap: _busy
                ? null
                : () => _control(run.state == 'paused' ? 'resume' : 'pause'),
          ),
        if (!run.terminal && run.state != 'canceling')
          TeamActionLink(
            label: i18n.t('teams:cancelRun'),
            muted: true,
            onTap: _busy ? null : () => _control('cancel'),
          ),
        if (run.finalSummary.isNotEmpty)
          TeamActionLink(
            label: i18n.t('teams:copyFinal'),
            onTap: () async {
              final toast = ref.read(toastProvider.notifier);
              await Clipboard.setData(ClipboardData(text: run.finalSummary));
              toast.success(i18n.t('teams:finalCopied'));
            },
          ),
      ],
    );
  }
}

/// Why a run is waiting or stopped, in the user's words (web
/// `TeamRunAttention`). Never silently absent: a paused run that explains
/// nothing reads as a hang.
class TeamAttention extends ConsumerWidget {
  const TeamAttention({
    super.key,
    required this.snapshot,
    this.notices = false,
  });

  final TeamSnapshot snapshot;
  final bool notices;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final run = snapshot.run;
    // An unrecognized code still has to say something: fall back to the
    // server's own wording rather than printing a translation key.
    String translate(String key, String fallback) {
      final line = i18n.t(key);
      return line == key ? fallback : line;
    }

    final visible = notices
        ? snapshot.notices
              .map(
                (notice) => translate(
                  'teams:notice.${asString(notice['code']) ?? ''}',
                  asString(notice['message']) ??
                      asString(notice['reason']) ??
                      '',
                ),
              )
              .where((line) => line.isNotEmpty)
              .toList()
        : const <String>[];
    final lines = [
      if (run.capacityRetryAt != null &&
          const ['running', 'waiting'].contains(run.state))
        i18n.t('teams:capacityWait'),
      if (run.pauseReason != null)
        translate('teams:reason.${run.pauseReason}', run.pauseReason!),
      if (run.failureReason?.isNotEmpty == true) run.failureReason!,
      ...visible,
    ];
    if (lines.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (final line in lines)
            Padding(
              padding: const EdgeInsets.only(bottom: 2),
              child: Text(
                line,
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  color: t.a700,
                  height: 1.6,
                ),
              ),
            ),
        ],
      ),
    );
  }
}

/// The team progress card in the conversation (web `TeamProgressCard`), built
/// on the same frame, status marks and progress bar as the ordinary task card
/// (§13.7): heading with counter and state, the task list with its owner and
/// state per row, and the run's controls along the bottom.
class TeamProgressCard extends ConsumerStatefulWidget {
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
  ConsumerState<TeamProgressCard> createState() => _TeamProgressCardState();
}

class _TeamProgressCardState extends ConsumerState<TeamProgressCard> {
  bool _open = true;

  @override
  Widget build(BuildContext context) {
    final key = (scope: widget.scope, runId: widget.runId);
    final value = ref.watch(teamRunProvider(key));
    final snapshot = value.valueOrNull;
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);

    if (snapshot == null) {
      return TaskCardFrame(
        child: value.hasError
            ? Row(
                children: [
                  Expanded(
                    child: Text(
                      errorText(i18n, value.error!),
                      style: TextStyle(fontSize: FontSizes.sm, color: t.danger),
                    ),
                  ),
                  TeamActionLink(
                    label: i18n.t('common:action.retry'),
                    onTap: () => ref.invalidate(teamRunProvider(key)),
                  ),
                ],
              )
            : SizedBox(
                height: 4,
                child: LinearProgressIndicator(
                  color: t.accent,
                  backgroundColor: t.hair,
                ),
              ),
      );
    }

    final run = snapshot.run;
    final total = snapshot.taskCount;
    final done = snapshot.completedTaskCount;
    final ratio = total == 0 ? 0.0 : (done / total).clamp(0.0, 1.0);

    return TaskCardFrame(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Icon(Icons.groups_outlined, size: 16, color: t.a700),
              const SizedBox(width: 8),
              Expanded(
                child: InkWell(
                  onTap: () => setState(() => _open = !_open),
                  child: Row(
                    children: [
                      Expanded(
                        child: Text(
                          run.title,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontSize: FontSizes.base,
                            fontWeight: FontWeight.w500,
                            color: t.ink,
                          ),
                        ),
                      ),
                      const SizedBox(width: 6),
                      AnimatedRotation(
                        turns: _open ? 0.5 : 0,
                        duration: const Duration(milliseconds: 200),
                        child: Icon(Icons.expand_more, size: 15, color: t.n500),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(width: 8),
              TeamStatePill(label: i18n.t('teams:state.${run.state}')),
            ],
          ),
          // The counter and the roster size go on their own line: on a phone
          // the web card's single heading row leaves a long goal three
          // readable characters. They stay outside the fold, so a collapsed
          // card still says how far along the run is.
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text(
              [
                i18n.t('teams:taskCount', vars: {'done': done, 'total': total}),
                i18n.t('teams:membersValue', count: snapshot.members.length),
              ].join(' · '),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
          ),
          Fold(
            open: _open,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const SizedBox(height: 10),
                ClipRRect(
                  borderRadius: BorderRadius.circular(Radii.full),
                  child: SizedBox(
                    height: 4,
                    child: Stack(
                      children: [
                        ColoredBox(
                          color: t.n300,
                          child: const SizedBox.expand(),
                        ),
                        AnimatedFractionallySizedBox(
                          duration: const Duration(milliseconds: 400),
                          alignment: AlignmentDirectional.centerStart,
                          widthFactor: ratio,
                          child: ColoredBox(
                            color: t.accent,
                            child: const SizedBox.expand(),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 6),
                // The card is a summary: a long run keeps it readable by
                // leaving the tail to the run screen behind "查看详情".
                for (final task in snapshot.tasks.take(8))
                  _TaskLine(task: task, members: snapshot.members),
                TeamAttention(snapshot: snapshot),
              ],
            ),
          ),
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: TeamControls(
              scope: widget.scope,
              run: run,
              onDetails: widget.onDetails,
            ),
          ),
          if (value.hasError)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                errorText(i18n, value.error!),
                style: TextStyle(fontSize: FontSizes.xs, color: t.danger),
              ),
            ),
        ],
      ),
    );
  }
}

class _TaskLine extends ConsumerWidget {
  const _TaskLine({required this.task, required this.members});

  final Map<String, dynamic> task;
  final List<Map<String, dynamic>> members;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final state = teamTaskState(task, members);
    final owner = members
        .where((member) => member['id'] == task['owner_member_id'])
        .firstOrNull;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 5),
      child: Row(
        children: [
          TeamStatusMark(state: state),
          const SizedBox(width: 11),
          Expanded(
            child: Text(
              asString(task['title']) ?? '',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontSize: FontSizes.base,
                fontWeight: state == 'running'
                    ? FontWeight.w500
                    : FontWeight.w400,
                color: state == 'succeeded' ? t.n700 : t.ink,
              ),
            ),
          ),
          if (owner != null) ...[
            const SizedBox(width: 8),
            ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 88),
              child: Text(
                asString(owner['name']) ?? asString(owner['alias']) ?? '',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
            ),
          ],
          const SizedBox(width: 8),
          ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 72),
            child: Text(
              i18n.t('teams:state.$state'),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
          ),
        ],
      ),
    );
  }
}
