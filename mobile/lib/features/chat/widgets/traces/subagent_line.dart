import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/message_part.dart';
import '../../../../shared/utils/format.dart';
import '../../../../shared/widgets/shimmer_text.dart';
import '../../state/subagent_progress.dart';
import '../../utils/tool_map.dart';

/// One line under a `task` row saying what the subagent is doing
/// (web `SubagentLine`).
///
/// A task row that only says "调用中" tells you nothing for however many
/// minutes the child takes, and a subagent's time is usually the longest
/// stretch in a turn.
class SubagentLine extends ConsumerWidget {
  const SubagentLine({super.key, required this.part});

  final ToolPart part;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final progress = ref.watch(subagentProgressProvider(part));
    // Nothing to add before the child has reported anything: the row above
    // already says the call is running.
    if (progress.sessionId == null || progress.toolCount == 0) {
      return const SizedBox.shrink();
    }

    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final running =
        part.status == ToolStatus.running || part.status == ToolStatus.pending;
    final current = progress.current;
    final detail = running && current != null
        ? '${i18n.t('chat:kind.${toolKindKey(current.tool)}')} '
            '${toolTarget(current)}'
        : i18n.t('chat:subagent.summary',
            count: progress.toolCount,
            vars: {'duration': formatDuration(progress.seconds)});

    final style = TextStyle(
      fontSize: FontSizes.xs,
      color: t.n600,
      fontFamily: 'Menlo',
      fontFamilyFallback: const ['monospace'],
    );

    return Padding(
      padding: const EdgeInsets.only(top: 2, bottom: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('↳ ', style: TextStyle(fontSize: FontSizes.xs, color: t.n500)),
          Expanded(
            child: running
                ? ShimmerText(detail, style: style, maxLines: 1)
                : Text(
                    detail,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: style,
                  ),
          ),
        ],
      ),
    );
  }
}
