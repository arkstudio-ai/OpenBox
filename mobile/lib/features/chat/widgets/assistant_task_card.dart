import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/router/paths.dart';
import '../api/assistant_api.dart';
import 'assistant_report.dart';

const _stateKeys = {
  'paused': 'chat:assistant.task.state.paused',
  'pausing': 'chat:assistant.task.state.pausing',
  'resuming': 'chat:assistant.task.state.resuming',
  'resume_blocked': 'chat:assistant.task.state.resumeBlocked',
  'canceled': 'chat:assistant.task.state.canceled',
  'canceling': 'chat:assistant.task.state.canceling',
  'effect_unknown': 'chat:assistant.task.state.effectUnknown',
  'running': 'chat:assistant.task.state.running',
  'idle': 'chat:assistant.task.state.idle',
  'queued': 'chat:assistant.task.state.queued',
  'waiting_input': 'chat:assistant.task.state.waitingInput',
  'completed': 'chat:assistant.task.state.completed',
  'error': 'chat:assistant.task.state.error',
  'aborted': 'chat:assistant.task.state.aborted',
  'input_not_applied': 'chat:assistant.steerNotApplied',
};
const _controlKeys = {
  'pause': 'chat:assistant.task.control.pause',
  'resume': 'chat:assistant.task.control.resume',
  'cancel': 'chat:assistant.task.control.cancel',
};

class AssistantTaskCard extends ConsumerStatefulWidget {
  const AssistantTaskCard({
    super.key,
    required this.task,
    required this.scope,
    required this.lastSeen,
    this.pending,
    this.commandId,
    this.selectedResult,
    required this.onControl,
    required this.onRetry,
  });
  final AssistantTask task;
  final AssistantScope scope;
  final int lastSeen;
  final String? pending;
  final String? commandId;
  final Map<String, dynamic>? selectedResult;
  final Future<void> Function(String action) onControl;
  final Future<void> Function() onRetry;
  @override
  ConsumerState<AssistantTaskCard> createState() => _AssistantTaskCardState();
}

class _AssistantTaskCardState extends ConsumerState<AssistantTaskCard> {
  bool _busy = false;
  Future<void> _run(Future<void> Function() action) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await action();
    } catch (_) {
      /* The parent owns the error toast. */
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final task = widget.task;
    final result = widget.selectedResult ?? task.result;
    final delivery = result['delivery_state'];
    final resultId = asString(result['result_id']);
    final sequence = asInt(result['processed_sequence']);
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 0, 12, 12),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        border: Border.all(color: context.tokens.hair),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(task.title, style: const TextStyle(fontWeight: FontWeight.w600)),
          Text(
            '${i18n.t('chat:assistant.task.currentState')} ${i18n.t(_stateKeys[task.observed] ?? 'chat:assistant.task.state.unknown')}',
          ),
          if (task.observed == 'effect_unknown')
            Text(i18n.t('chat:assistant.task.checkEffects')),
          if (widget.commandId != null)
            ExpansionTile(
              key: PageStorageKey((widget.scope, widget.commandId)),
              title: Text(i18n.t('chat:assistant.receipt')),
              children: [Text(widget.commandId!)],
            ),
          if (asMap(task.data['latest_control'])['command_id'] is String)
            ExpansionTile(
              title: Text(i18n.t('chat:assistant.task.controlReceipt')),
              children: [
                Text(
                  asMap(task.data['latest_control'])['command_id'] as String,
                ),
              ],
            ),
          if (task.submission.isNotEmpty)
            Text(
              i18n.t(
                task.submission['disposition'] == 'not_applied'
                    ? 'chat:assistant.steerNotApplied'
                    : task.submission['state'] == 'canceled' &&
                          asMap(task.submission['error'])['code'] ==
                              'ASSISTANT_ASSET_UNAVAILABLE'
                    ? 'chat:assistant.inputAssetUnavailable'
                    : task.submission['state'] == 'canceled' &&
                          task.submission['applied_at'] == null
                    ? 'chat:assistant.inputCanceled'
                    : task.submission['delivery'] == 'steer'
                    ? (task.submission['applied_at'] != null
                          ? 'chat:assistant.steerApplied'
                          : 'chat:assistant.steerAccepted')
                    : task.submission['applied_at'] != null
                    ? 'chat:assistant.inputApplied'
                    : task.submission['state'] == 'canceled'
                    ? 'chat:assistant.inputCanceled'
                    : 'chat:assistant.inputAccepted',
              ),
            ),
          if (result.isNotEmpty) ...[
            if (asInt(result['observed_intent_revision']) !=
                    asInt(task.task['intent_revision']) ||
                result['result_id'] != task.result['result_id'])
              Text(i18n.t('chat:assistant.earlierResult')),
            Text(
              '${i18n.t('chat:assistant.execution')}: ${i18n.t(switch (result['outcome']) {
                'succeeded' => 'chat:assistant.executionSucceeded',
                'aborted' => 'chat:assistant.executionStopped',
                _ => 'chat:assistant.executionFailed',
              })}',
            ),
            Text(
              '${i18n.t('chat:assistant.acceptance')}: ${i18n.t(result['assistant_inbox_id'] != null ? 'chat:assistant.accepted' : 'chat:assistant.pending')}',
            ),
            Text(
              '${i18n.t('chat:assistant.processing')}: ${i18n.t(delivery == 'processed'
                  ? 'chat:assistant.processed'
                  : delivery == 'blocked'
                  ? 'chat:assistant.reportBlocked'
                  : 'chat:assistant.pending')}',
            ),
            Text(
              '${i18n.t('chat:assistant.reading')}: ${i18n.t(sequence == null
                  ? 'chat:assistant.noAnswer'
                  : widget.lastSeen >= sequence
                  ? 'chat:assistant.read'
                  : 'chat:assistant.unread')}',
            ),
            if (delivery == 'retry_wait' || delivery == 'blocked')
              Text(i18n.t('chat:assistant.reportFailure')),
            if (delivery == 'retry_wait' || delivery == 'blocked')
              TextButton(
                onPressed: _busy ? null : () => _run(widget.onRetry),
                child: Text(i18n.t('chat:assistant.retryReport')),
              ),
          ],
          Wrap(
            spacing: 4,
            children: [
              if (widget.pending != null)
                TextButton(
                  onPressed: _busy
                      ? null
                      : () => _run(() => widget.onControl(widget.pending!)),
                  child: Text(
                    '${i18n.t('chat:assistant.reload')} · ${i18n.t(_controlKeys[widget.pending] ?? 'chat:assistant.task.controlReceipt')}',
                  ),
                )
              else if (task.desired != 'canceled') ...[
                TextButton(
                  onPressed:
                      _busy ||
                          (task.desired == 'paused' &&
                              !const {
                                'paused',
                                'resume_blocked',
                              }.contains(task.observed))
                      ? null
                      : () => _run(
                          () => widget.onControl(
                            task.desired == 'paused' ? 'resume' : 'pause',
                          ),
                        ),
                  child: Text(
                    i18n.t(
                      task.desired == 'paused'
                          ? 'chat:assistant.task.control.resume'
                          : 'chat:assistant.task.control.pause',
                    ),
                  ),
                ),
                TextButton(
                  onPressed: _busy
                      ? null
                      : () => _run(() => widget.onControl('cancel')),
                  child: Text(i18n.t('chat:assistant.task.control.cancel')),
                ),
              ],
              TextButton(
                onPressed: () => context.push(Paths.chat(task.executionId)),
                child: Text(i18n.t('chat:assistant.openTask')),
              ),
              if (resultId != null)
                TextButton(
                  onPressed: () => showModalBottomSheet<void>(
                    context: context,
                    isScrollControlled: true,
                    builder: (_) => AssistantReport(
                      scope: widget.scope,
                      resultId: resultId,
                    ),
                  ),
                  child: Text(i18n.t('chat:assistant.originalReport')),
                ),
            ],
          ),
        ],
      ),
    );
  }
}
