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
/// (web `ProcessingNotice`). Each is one line; the turns that could not be
/// saved open in a sheet, in the person's own words, to retry or dismiss.
class ProcessingNotice extends ConsumerWidget {
  const ProcessingNotice({
    super.key,
    required this.projectId,
    required this.processing,
    required this.onOpenChat,
  });

  final String projectId;
  final MemoryProcessing? processing;
  final ValueChanged<String> onOpenChat;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final pending = processing?.pending ?? 0;
    final failed = processing?.failed ?? const <FailedTurn>[];
    if (pending == 0 && failed.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (pending > 0)
          Padding(
            padding: const EdgeInsets.only(top: 12),
            child: KnowledgeBanner(
              key: const ValueKey('knowledge-processing'),
              live: true,
              leading: const Spinner(size: 13),
              text: i18n.t(
                'knowledge:processing.pending',
                vars: {'count': pending},
              ),
            ),
          ),
        if (failed.isNotEmpty)
          Padding(
            padding: EdgeInsets.only(top: pending > 0 ? 8 : 12),
            child: KnowledgeBanner(
              key: const ValueKey('knowledge-failed'),
              leading: Icon(Icons.error_outline, size: 17, color: t.dangerInk),
              text: i18n.t(
                'knowledge:processing.failedTitle',
                vars: {'count': failed.length},
              ),
              action: i18n.t('knowledge:processing.show'),
              onAction: () => showModalBottomSheet<void>(
                context: context,
                isScrollControlled: true,
                useSafeArea: true,
                showDragHandle: true,
                backgroundColor: t.card,
                builder: (_) => _FailedTurnsSheet(
                  projectId: projectId,
                  onOpenChat: onOpenChat,
                ),
              ),
            ),
          ),
      ],
    );
  }
}

class _FailedTurnsSheet extends ConsumerStatefulWidget {
  const _FailedTurnsSheet({required this.projectId, required this.onOpenChat});

  final String projectId;
  final ValueChanged<String> onOpenChat;

  @override
  ConsumerState<_FailedTurnsSheet> createState() => _FailedTurnsSheetState();
}

class _FailedTurnsSheetState extends ConsumerState<_FailedTurnsSheet> {
  bool _acting = false;
  bool _closing = false;

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

  void _close() {
    if (_closing) return;
    _closing = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) Navigator.of(context).maybePop();
    });
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final state = ref.watch(memoryProcessingProvider(widget.projectId));
    final failed = state.current?.failed;
    // Nothing left to look at once every turn is retried or dismissed.
    if (failed != null && failed.isEmpty && !state.isLoading) _close();
    final items = failed ?? const <FailedTurn>[];
    return ConstrainedBox(
      constraints: BoxConstraints(
        maxHeight: MediaQuery.sizeOf(context).height * 0.8,
      ),
      child: SafeArea(
        top: false,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 0, 20, 4),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    i18n.t(
                      'knowledge:processing.failedTitle',
                      vars: {'count': items.length},
                    ),
                    style: TextStyle(
                      fontSize: FontSizes.lg,
                      fontWeight: FontWeight.w500,
                      color: t.ink,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    i18n.t('knowledge:processing.failedHint'),
                    style: TextStyle(
                      fontSize: FontSizes.sm,
                      height: 1.55,
                      color: t.n600,
                    ),
                  ),
                ],
              ),
            ),
            Flexible(
              child: ListView(
                shrinkWrap: true,
                padding: const EdgeInsets.symmetric(horizontal: 20),
                children: [
                  for (final item in items)
                    _FailedTurnRow(
                      key: ValueKey('failed-turn-${item.id}'),
                      item: item,
                      acting: _acting,
                      onOpenChat: () {
                        Navigator.of(context).pop();
                        widget.onOpenChat(item.sessionId);
                      },
                      onRetry: () => _act([item.id], retry: true),
                      onDismiss: () => _act([item.id], retry: false),
                    ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 12, 20, 12),
              child: Align(
                alignment: Alignment.centerRight,
                child: KnowledgeButton(
                  tone: PillTone.primary,
                  label: i18n.t('knowledge:processing.retryAll'),
                  onPressed: _acting || items.isEmpty
                      ? null
                      : () => _act([
                          for (final item in items) item.id,
                        ], retry: true),
                ),
              ),
            ),
          ],
        ),
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
  final VoidCallback onOpenChat;
  final VoidCallback onRetry;
  final VoidCallback onDismiss;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final action = TextStyle(fontSize: FontSizes.sm, color: t.n800);
    return Container(
      margin: const EdgeInsets.only(top: 12),
      padding: const EdgeInsets.only(top: 12),
      decoration: BoxDecoration(
        border: Border(top: BorderSide(color: t.hair)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            '“${item.excerpt}”',
            style: TextStyle(
              fontSize: FontSizes.base,
              height: 1.55,
              color: t.ink,
            ),
          ),
          const SizedBox(height: 2),
          Wrap(
            spacing: 4,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              TextButton.icon(
                onPressed: onOpenChat,
                style: TextButton.styleFrom(
                  padding: const EdgeInsets.symmetric(horizontal: 4),
                ),
                icon: Icon(Icons.chat_bubble_outline, size: 13, color: t.a700),
                label: Text(
                  (item.sessionTitle ?? '').isNotEmpty
                      ? item.sessionTitle!
                      : i18n.t('knowledge:processing.untitled'),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.a700),
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
