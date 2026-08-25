import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/message_part.dart';

/// Thin centered notice rule (web `StepDivider`) for compaction / retry /
/// agent-switch parts.
class StepDivider extends ConsumerWidget {
  const StepDivider({super.key, required this.part});

  final MessagePart part;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final label = switch (part) {
      CompactionPart() => i18n.t('chat:compaction'),
      RetryPart(:final attempt) =>
        i18n.t('chat:retry', vars: {'attempt': attempt}),
      AgentPart(:final agent) =>
        i18n.t('chat:agentSwitch', vars: {'agent': agent}),
      _ => '',
    };
    if (label.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 10),
      child: Row(
        children: [
          Expanded(child: Divider(color: t.hair, height: 1)),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 10),
            child: Text(
              label,
              style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
            ),
          ),
          Expanded(child: Divider(color: t.hair, height: 1)),
        ],
      ),
    );
  }
}
