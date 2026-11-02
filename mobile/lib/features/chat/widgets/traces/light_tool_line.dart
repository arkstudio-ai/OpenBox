import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/message_part.dart';
import '../../../../shared/widgets/shimmer_text.dart';
import '../../../../shared/widgets/spinner.dart';
import '../../utils/light_tool_names.dart';
import '../../utils/turn_view.dart';

/// The tool chain as Light 模式 shows it (docs `LIGHT_MODE_PLAN.md` §4.4):
/// one line — "正在做：生成视频" while it runs, "做了 3 步" after — with
/// nothing to unfold. The pro chat keeps the full timeline.
class LightToolLine extends ConsumerWidget {
  const LightToolLine({super.key, required this.turn, required this.active});

  final AssistantTurnData turn;
  final bool active;

  MessagePart? get _current {
    final running = turn.toolChain.where(
      (p) =>
          p is ToolPart &&
          (p.status == ToolStatus.running || p.status == ToolStatus.pending),
    );
    if (running.isNotEmpty) return running.last;
    return turn.toolChain.isEmpty ? null : turn.toolChain.last;
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final text = active
        ? i18n.t(
            'light:tool.doing',
            vars: {'action': i18n.t('light:tool.${lightToolKey(_current)}')},
          )
        : i18n.t('light:tool.done', count: turn.toolChain.length);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        children: [
          if (active)
            PulseDot(color: t.a700)
          else
            Icon(Icons.check_circle_outline, size: 14, color: t.n500),
          const SizedBox(width: 8),
          Expanded(
            child: ShimmerText(
              text,
              enabled: active,
              style: TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w500,
                color: t.n600,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
