import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/message_part.dart';
import '../../../../shared/widgets/spinner.dart';
import '../../utils/tool_map.dart';
import '../../utils/turn_view.dart';
import 'subagent_line.dart';
import 'tool_output.dart';
import 'trace_shell.dart';

/// Tool-chain trace (web `ToolChainTrace`): timeline with a connector rail,
/// per-call dot (danger=failed, accent pulse=running, n500=done), kind label
/// + detail, tap to expand request/response.
class ToolChainTrace extends ConsumerWidget {
  const ToolChainTrace({
    super.key,
    required this.turn,
    required this.active,
    required this.autoCollapseReady,
  });

  final AssistantTurnData turn;

  /// Held live for the whole tool phase — `toolsStreaming` drops in the gap
  /// between two calls, which made the row title flicker (web parity).
  final bool active;
  final bool autoCollapseReady;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    return TraceShell(
      title: active
          ? i18n.t('chat:trace.tool.titleActive')
          : i18n.t('chat:trace.tool.titleDone'),
      summary: i18n
          .t('chat:trace.tool.summaryCount', count: turn.toolChain.length),
      active: active,
      autoCollapseReady: autoCollapseReady,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          for (final (index, part) in turn.toolChain.indexed)
            _ToolRow(part: part, isLast: index == turn.toolChain.length - 1),
        ],
      ),
    );
  }
}

class _ToolRow extends ConsumerStatefulWidget {
  const _ToolRow({required this.part, required this.isLast});

  final MessagePart part;
  final bool isLast;

  @override
  ConsumerState<_ToolRow> createState() => _ToolRowState();
}

class _ToolRowState extends ConsumerState<_ToolRow> {
  bool _expanded = false;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final part = widget.part;

    final (label, detail, status) = switch (part) {
      ToolPart() => (
          i18n.t('chat:kind.${toolKindKey(part.tool)}'),
          toolDetail(part),
          part.status,
        ),
      SubtaskPart() => (
          i18n.t('chat:kind.task'),
          part.description,
          part.status == 'error' ? ToolStatus.error : ToolStatus.completed,
        ),
      _ => ('', '', ToolStatus.completed),
    };

    final dot = switch (status) {
      ToolStatus.error => Container(
          width: 7,
          height: 7,
          decoration: BoxDecoration(color: t.danger, shape: BoxShape.circle),
        ),
      ToolStatus.running || ToolStatus.pending => PulseDot(color: t.a700),
      ToolStatus.completed => Container(
          width: 7,
          height: 7,
          decoration: BoxDecoration(color: t.n500, shape: BoxShape.circle),
        ),
    };

    return InkWell(
      onTap: () => setState(() => _expanded = !_expanded),
      child: Padding(
        padding: const EdgeInsets.only(bottom: 2),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 16,
              child: Column(
                children: [
                  const SizedBox(height: 6),
                  dot,
                  if (!widget.isLast)
                    Container(width: 1, height: 18, color: t.hair),
                ],
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Text(
                        label,
                        style: TextStyle(
                          fontSize: FontSizes.sm,
                          fontWeight: FontWeight.w500,
                          color: t.n800,
                        ),
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          detail,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontSize: FontSizes.sm,
                            color: t.n600,
                            fontFamily: 'Menlo',
                            fontFamilyFallback: const ['monospace'],
                          ),
                        ),
                      ),
                      if (status == ToolStatus.error)
                        Text(
                          i18n.t('chat:toolStatus.failed'),
                          style: TextStyle(
                              fontSize: FontSizes.xs, color: t.danger),
                        ),
                    ],
                  ),
                  // A task row is silent for as long as its subagent works,
                  // which is usually the longest thing in the chain.
                  if (part is ToolPart && part.tool == 'task')
                    SubagentLine(part: part),
                  if (_expanded) ToolDetailBox(part: part),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Expandable detail body for one tool call — shared by the flat chain and
/// the task card's per-task rows.
///
/// The phone keeps the tap-to-expand the web does not need (a row here is one
/// line wide), but what opens is the same structured column: a shell call
/// reads as its command and output, a skill load as just its name.
class ToolDetailBox extends StatelessWidget {
  const ToolDetailBox({super.key, required this.part});

  final MessagePart part;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      margin: const EdgeInsets.only(top: 6, bottom: 6),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: t.n200.withValues(alpha: 0.5),
        borderRadius: BorderRadius.circular(Radii.md),
      ),
      child: ToolOutput(part: part),
    );
  }
}
