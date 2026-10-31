import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/session.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/toast.dart';
import '../api/assistant_api.dart';
import '../state/effect_ack.dart';

/// "我已核对，继续" on the execution page itself (web `ExecutionEffectNotice`).
///
/// A task stopped mid-step leaves an unverified external outcome; until the
/// owner confirms it, every message sent here is refused with
/// `ASSISTANT_EFFECT_UNRESOLVED`. The only control used to be a button on the
/// task card in the assistant conversation, which the App did not have at all,
/// so people were told to press a button that was not there (2026-10-31).
/// This sits above the composer whenever the session's task reports
/// `effect_unknown`, or a send from this page was just refused for it.
/// Nothing is resent; the person sends their message again.
class ExecutionEffectNotice extends ConsumerStatefulWidget {
  const ExecutionEffectNotice({
    super.key,
    required this.sessionId,
    required this.session,
    this.onAcknowledged,
  });

  final String sessionId;
  final Session? session;

  /// Re-reads the session after a confirmation changed its task state.
  final VoidCallback? onAcknowledged;

  static bool visible(Session? session, bool requested) {
    final control = session?.taskControl;
    if (session == null ||
        !session.assistantManaged ||
        session.kind == 'assistant' ||
        control == null) {
      return false;
    }
    return control['observed_state'] == 'effect_unknown' || requested;
  }

  @override
  ConsumerState<ExecutionEffectNotice> createState() =>
      _ExecutionEffectNoticeState();
}

class _ExecutionEffectNoticeState extends ConsumerState<ExecutionEffectNotice> {
  bool _busy = false;
  // One key per attempt chain: a lost response is retried as the same command.
  String? _key;

  String _newKey() {
    final random = Random.secure();
    return 'ack-${List.generate(24, (_) => random.nextInt(256).toRadixString(16).padLeft(2, '0')).join()}';
  }

  Future<void> _confirm() async {
    final control = widget.session?.taskControl;
    final scope = ref.read(assistantScopeProvider);
    if (_busy || control == null) return;
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    if (scope == null) {
      toast.error(i18n.t('errors:fallback'));
      return;
    }
    setState(() => _busy = true);
    _key ??= _newKey();
    try {
      final receipt = await ref
          .read(assistantApiProvider(scope))
          .acknowledgeEffects(control['task_id'] as String, {
            'idempotency_key': _key,
            'expected_revision': control['expected_revision'],
          });
      if (!mounted) return;
      _key = null;
      ref
          .read(executionEffectAckRequestedProvider(widget.sessionId).notifier)
          .state = false;
      toast.info(
        i18n.t(
          receipt['remaining_unresolved'] == true
              ? 'chat:execution.effectAck.pending'
              : 'chat:execution.effectAck.done',
        ),
      );
      widget.onAcknowledged?.call();
    } catch (error) {
      if (!mounted) return;
      // A definite refusal (stale revision, conflict) gets a fresh command
      // next time; a transport failure keeps the key so a retry replays.
      final status = apiErrorOf(error)?.status ?? 0;
      if (status >= 400 && status < 500) _key = null;
      toast.error(errorText(i18n, error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final requested = ref.watch(
      executionEffectAckRequestedProvider(widget.sessionId),
    );
    if (!ExecutionEffectNotice.visible(widget.session, requested)) {
      return const SizedBox.shrink();
    }
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return _NoticeFrame(
      title: i18n.t('chat:execution.effectAck.title'),
      body: i18n.t('chat:execution.effectAck.body'),
      actions: [
        OutlinedButton(
          onPressed: _busy ? null : _confirm,
          style: OutlinedButton.styleFrom(
            foregroundColor: t.ink,
            side: BorderSide(color: t.hair),
            visualDensity: VisualDensity.compact,
          ),
          child: Text(i18n.t('chat:execution.effectAck.action')),
        ),
      ],
    );
  }
}

/// Stopping a task-linked conversation cancels the whole task: ask once,
/// inline, before doing it (web `StopTaskNotice`).
class StopTaskNotice extends ConsumerWidget {
  const StopTaskNotice({
    super.key,
    required this.onConfirm,
    required this.onKeep,
  });

  final VoidCallback onConfirm;
  final VoidCallback onKeep;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return _NoticeFrame(
      title: i18n.t('chat:execution.stopTask.title'),
      body: i18n.t('chat:execution.stopTask.body'),
      actions: [
        FilledButton(
          onPressed: onConfirm,
          style: FilledButton.styleFrom(
            backgroundColor: t.danger,
            foregroundColor: t.bg,
            visualDensity: VisualDensity.compact,
          ),
          child: Text(i18n.t('chat:execution.stopTask.confirm')),
        ),
        OutlinedButton(
          onPressed: onKeep,
          style: OutlinedButton.styleFrom(
            foregroundColor: t.ink,
            side: BorderSide(color: t.hair),
            visualDensity: VisualDensity.compact,
          ),
          child: Text(i18n.t('chat:execution.stopTask.keep')),
        ),
      ],
    );
  }
}

class _NoticeFrame extends StatelessWidget {
  const _NoticeFrame({
    required this.title,
    required this.body,
    required this.actions,
  });

  final String title;
  final String body;
  final List<Widget> actions;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 0, 20, 6),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: 3),
            child: Icon(Icons.error_outline, size: 14, color: t.danger),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  title,
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w600,
                    color: t.ink,
                    height: 1.5,
                  ),
                ),
                Text(
                  body,
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    color: t.n600,
                    height: 1.5,
                  ),
                ),
                const SizedBox(height: 6),
                Wrap(spacing: 8, runSpacing: 4, children: actions),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
