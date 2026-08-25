import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/i18n/i18n.dart';
import '../../utils/turn_view.dart';
import '../markdown_view.dart';
import 'trace_shell.dart';

/// Thinking trace (web `ThinkingTrace`): collapsible muted markdown of the
/// reasoning text.
class ThinkingTrace extends ConsumerWidget {
  const ThinkingTrace({super.key, required this.turn});

  final AssistantTurnData turn;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    return TraceShell(
      title: turn.thinkingStreaming
          ? i18n.t('chat:trace.think.titleActive')
          : i18n.t('chat:trace.think.titleDone'),
      summary: turn.thinkingStreaming
          ? i18n.t('chat:trace.think.subtitleActive')
          : i18n.t('chat:trace.think.subtitleDone'),
      active: turn.thinkingStreaming,
      autoCollapseReady: turn.hasBody,
      child: MarkdownView(
        turn.thinkingText,
        variant: MarkdownVariant.thinking,
        streaming: turn.thinkingStreaming,
      ),
    );
  }
}
