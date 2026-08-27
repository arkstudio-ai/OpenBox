import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/utils/format.dart';
import '../../utils/turn_view.dart';
import 'trace_shell.dart';

/// Process trace (web `ProcessTrace`): step count, context tokens, duration.
class ProcessTrace extends ConsumerWidget {
  const ProcessTrace({
    super.key,
    required this.turn,
    required this.active,
    required this.autoCollapseReady,
  });

  final AssistantTurnData turn;
  final bool active;

  /// Only an actual final answer closes the live rows — process narration
  /// must not collapse the trace or masquerade as an answer (web parity).
  final bool autoCollapseReady;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final duration = formatDuration(turn.durationSec);
    return TraceShell(
      title: active
          ? i18n.t('chat:trace.process.titleActive')
          : i18n.t('chat:trace.process.titleDone'),
      summary: i18n.t(
        'chat:processSummary',
        vars: {'duration': duration},
        count: turn.stepCount,
      ),
      active: active,
      autoCollapseReady: autoCollapseReady,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (turn.contextTokens > 0)
            _line(
              t,
              i18n.t('chat:trace.process.contextTokens',
                  vars: {'tokens': formatTokens(turn.contextTokens)}),
            ),
          if (turn.durationSec > 0)
            _line(
              t,
              i18n.t('chat:trace.process.duration', vars: {'duration': duration}),
            ),
        ],
      ),
    );
  }

  Widget _line(BossipTokens t, String text) => Padding(
        padding: const EdgeInsets.only(bottom: 4),
        child: Text(
          text,
          style: TextStyle(fontSize: FontSizes.sm, color: t.n600, height: 1.5),
        ),
      );
}
