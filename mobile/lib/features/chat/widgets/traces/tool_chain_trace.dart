import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/message_part.dart';
import '../../../../shared/widgets/spinner.dart';
import '../../utils/tool_map.dart';
import '../../utils/turn_view.dart';
import 'trace_shell.dart';

/// Tool-chain trace (web `ToolChainTrace`): timeline with a connector rail,
/// per-call dot (danger=failed, accent pulse=running, n500=done), kind label
/// + detail, tap to expand request/response.
class ToolChainTrace extends ConsumerWidget {
  const ToolChainTrace({super.key, required this.turn});

  final AssistantTurnData turn;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    return TraceShell(
      title: turn.toolsStreaming
          ? i18n.t('chat:trace.tool.titleActive')
          : i18n.t('chat:trace.tool.titleDone'),
      summary: i18n
          .t('chat:trace.tool.summaryCount', count: turn.toolChain.length),
      active: turn.toolsStreaming,
      autoCollapseReady: turn.hasBody,
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
      onTap: part is ToolPart
          ? () => setState(() => _expanded = !_expanded)
          : null,
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
                  if (_expanded && part is ToolPart)
                    _ToolDetail(part: part),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ToolDetail extends ConsumerWidget {
  const _ToolDetail({required this.part});

  final ToolPart part;

  static const _maxLines = 8;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final input = toolPayloadText(part.input);
    final output = part.error ?? toolPayloadText(part.output);
    return Container(
      margin: const EdgeInsets.only(top: 6, bottom: 6),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: t.n200.withValues(alpha: 0.5),
        borderRadius: BorderRadius.circular(Radii.md),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (input.isNotEmpty) ...[
            _label(t, i18n.t('chat:toolDetail.request')),
            _mono(t, input),
          ],
          if (output.isNotEmpty) ...[
            if (input.isNotEmpty) const SizedBox(height: 8),
            _label(
              t,
              part.error != null
                  ? i18n.t('chat:toolDetail.error')
                  : i18n.t('chat:toolDetail.response'),
            ),
            _mono(t, output, danger: part.error != null),
          ],
        ],
      ),
    );
  }

  Widget _label(BossipTokens t, String text) => Padding(
        padding: const EdgeInsets.only(bottom: 3),
        child: Text(
          text,
          style: TextStyle(
            fontSize: FontSizes.xs2,
            fontWeight: FontWeight.w600,
            color: t.n500,
            letterSpacing: 0.4,
          ),
        ),
      );

  Widget _mono(BossipTokens t, String text, {bool danger = false}) => Text(
        text,
        maxLines: _maxLines,
        overflow: TextOverflow.ellipsis,
        style: TextStyle(
          fontSize: FontSizes.xs,
          height: 1.6,
          color: danger ? t.dangerInk : t.n700,
          fontFamily: 'Menlo',
          fontFamilyFallback: const ['monospace'],
        ),
      );
}
