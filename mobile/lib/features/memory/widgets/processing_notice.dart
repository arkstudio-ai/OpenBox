import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/spinner.dart';
import '../../../shared/widgets/toast.dart';
import '../api/knowledge_api.dart';
import '../models/memory_models.dart';
import '../state/knowledge_providers.dart';
import 'knowledge_parts.dart';

/// What is still being saved, and what could not be — never lost silently
/// (web `ProcessingNotice`). A failed turn shows the person's own words, with
/// a retry or a dismissal.
class ProcessingNotice extends ConsumerStatefulWidget {
  const ProcessingNotice({
    super.key,
    required this.processing,
    required this.onOpenChat,
  });

  final MemoryProcessing? processing;
  final ValueChanged<String> onOpenChat;

  @override
  ConsumerState<ProcessingNotice> createState() => _ProcessingNoticeState();
}

class _ProcessingNoticeState extends ConsumerState<ProcessingNotice> {
  bool _open = false;
  bool _acting = false;

  Future<void> _act(List<String> ids, {required bool retry}) async {
    final api = ref.read(knowledgeApiProvider);
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    setState(() => _acting = true);
    try {
      for (final id in ids) {
        await (retry ? api.retryTurn(id) : api.dismissTurn(id));
      }
      toast.success(
        i18n.t(
          retry
              ? 'knowledge:processing.retried'
              : 'knowledge:processing.dismissed',
        ),
      );
    } catch (error) {
      toast.error(errorText(i18n, error));
    } finally {
      if (mounted) {
        setState(() => _acting = false);
        ref.invalidate(memoryProcessingProvider);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final pending = widget.processing?.pending ?? 0;
    final failed = widget.processing?.failed ?? const <FailedTurn>[];
    if (pending == 0 && failed.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.only(top: 14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (pending > 0)
            Semantics(
              liveRegion: true,
              child: Row(
                children: [
                  const Spinner(),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      i18n.t(
                        'knowledge:processing.pending',
                        vars: {'count': pending},
                      ),
                      style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                    ),
                  ),
                ],
              ),
            ),
          if (pending > 0 && failed.isNotEmpty) const SizedBox(height: 10),
          if (failed.isNotEmpty)
            KnowledgeCard(
              padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Padding(
                        padding: const EdgeInsets.only(top: 1),
                        child: Icon(
                          Icons.error_outline,
                          size: 17,
                          color: t.dangerInk,
                        ),
                      ),
                      const SizedBox(width: 9),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              i18n.t(
                                'knowledge:processing.failedTitle',
                                vars: {'count': failed.length},
                              ),
                              style: TextStyle(
                                fontSize: FontSizes.sm,
                                fontWeight: FontWeight.w500,
                                color: t.ink,
                              ),
                            ),
                            const SizedBox(height: 2),
                            Text(
                              i18n.t('knowledge:processing.failedHint'),
                              style: TextStyle(
                                fontSize: FontSizes.xs,
                                height: 1.5,
                                color: t.n600,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 10),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      KnowledgeButton(
                        compact: true,
                        label: i18n.t(
                          _open
                              ? 'knowledge:processing.hide'
                              : 'knowledge:processing.show',
                        ),
                        onPressed: () => setState(() => _open = !_open),
                      ),
                      KnowledgeButton(
                        compact: true,
                        tone: PillTone.primary,
                        label: i18n.t('knowledge:processing.retryAll'),
                        onPressed: _acting
                            ? null
                            : () => _act([
                                for (final item in failed) item.id,
                              ], retry: true),
                      ),
                    ],
                  ),
                  if (_open)
                    for (final item in failed)
                      _FailedTurnRow(
                        key: ValueKey('failed-turn-${item.id}'),
                        item: item,
                        acting: _acting,
                        onOpenChat: widget.onOpenChat,
                        onRetry: () => _act([item.id], retry: true),
                        onDismiss: () => _act([item.id], retry: false),
                      ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

class _FailedTurnRow extends ConsumerWidget {
  const _FailedTurnRow({
    super.key,
    required this.item,
    required this.acting,
    required this.onOpenChat,
    required this.onRetry,
    required this.onDismiss,
  });

  final FailedTurn item;
  final bool acting;
  final ValueChanged<String> onOpenChat;
  final VoidCallback onRetry;
  final VoidCallback onDismiss;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final action = TextStyle(fontSize: FontSizes.xs, color: t.n700);
    return Container(
      margin: const EdgeInsets.only(top: 10),
      padding: const EdgeInsets.only(top: 10),
      decoration: BoxDecoration(
        border: Border(top: BorderSide(color: t.hair)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            '“${item.excerpt}”',
            style: TextStyle(fontSize: FontSizes.sm, height: 1.6, color: t.ink),
          ),
          const SizedBox(height: 4),
          Wrap(
            spacing: 4,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              TextButton.icon(
                onPressed: () => onOpenChat(item.sessionId),
                icon: Icon(Icons.chat_bubble_outline, size: 12, color: t.a700),
                label: Text(
                  (item.sessionTitle ?? '').isNotEmpty
                      ? item.sessionTitle!
                      : i18n.t('knowledge:processing.untitled'),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.a700),
                ),
              ),
              TextButton(
                onPressed: acting ? null : onRetry,
                child: Text(
                  i18n.t('knowledge:processing.retry'),
                  style: action,
                ),
              ),
              TextButton(
                onPressed: acting ? null : onDismiss,
                child: Text(
                  i18n.t('knowledge:processing.dismiss'),
                  style: action,
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
