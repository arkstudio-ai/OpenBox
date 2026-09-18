import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../utils/compaction_view.dart';
import '../markdown_view.dart';
import 'trace_shell.dart';

/// Context optimization shares the turn's process area; summaries stay folded.
class CompactionTrace extends ConsumerWidget {
  const CompactionTrace({super.key, required this.item});

  final CompactionView item;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final status = i18n.t('chat:trace.compaction.${item.status.name}');
    return TraceShell(
      title: i18n.t('chat:trace.compaction.title'),
      summary: status,
      active: item.status == CompactionStatus.running,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxHeight: 320),
        child: SingleChildScrollView(
          child: Container(
            padding: const EdgeInsetsDirectional.only(start: 12),
            decoration: BoxDecoration(
              border: BorderDirectional(start: BorderSide(color: t.hair)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  status,
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
                const SizedBox(height: 8),
                if (item.summary.isNotEmpty)
                  MarkdownView(
                    item.summary,
                    variant: MarkdownVariant.thinking,
                    streaming: item.status == CompactionStatus.running,
                  )
                else
                  Text(i18n.t('chat:trace.compaction.noSummary')),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
