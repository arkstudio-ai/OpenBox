import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/i18n/i18n.dart';
import '../../utils/turn_view.dart';
import '../markdown_view.dart';
import 'trace_shell.dart';

/// Thinking trace (web `ThinkingTrace`): collapsible muted markdown of the
/// reasoning text.
class ThinkingTrace extends ConsumerWidget {
  const ThinkingTrace({
    super.key,
    required this.turn,
    required this.active,
  });

  final AssistantTurnData turn;

  /// Held live for the whole reasoning phase rather than derived per frame —
  /// `thinkingStreaming` flips every time a tool part lands after reasoning,
  /// which made the title flicker between 正在思考 and 思考完成.
  final bool active;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    return TraceShell(
      title: active
          ? i18n.t('chat:trace.think.titleActive')
          : i18n.t('chat:trace.think.titleDone'),
      summary: active
          ? i18n.t('chat:trace.think.subtitleActive')
          : i18n.t('chat:trace.think.subtitleDone'),
      active: active,
      child: MarkdownView(
        turn.thinkingText,
        variant: MarkdownVariant.thinking,
        streaming: active,
      ),
    );
  }
}
