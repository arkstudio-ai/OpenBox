import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/message_part.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/toast.dart';
import '../api/assistant_api.dart';
import '../utils/memory_receipt.dart';

/// What the assistant remembered, updated or forgot in a turn, as compact
/// chips under its answer (web `AssistantMemoryReceipts`). A fresh memory
/// can be undone right here.
class AssistantMemoryReceipts extends ConsumerWidget {
  const AssistantMemoryReceipts({
    super.key,
    required this.scope,
    required this.parts,
  });
  final AssistantScope scope;
  final List<MessagePart> parts;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final receipts = [
      for (final part in parts)
        if (memoryReceipt(part) case final receipt?) (part.id, receipt),
    ];
    if (receipts.isEmpty) return const SizedBox.shrink();
    return Semantics(
      container: true,
      label: ref.watch(i18nProvider).t('chat:assistant.memory.label'),
      child: Padding(
        padding: const EdgeInsets.only(top: 8),
        child: Wrap(
          spacing: 6,
          runSpacing: 6,
          children: [
            for (final (partId, receipt) in receipts)
              receipt.kind == MemoryReceiptKind.remembered
                  ? _RememberedChip(
                      key: ValueKey(partId),
                      scope: scope,
                      partId: partId,
                      receipt: receipt,
                    )
                  : _Chip(key: ValueKey(partId), receipt: receipt),
          ],
        ),
      ),
    );
  }
}

class _ChipShell extends StatelessWidget {
  const _ChipShell({required this.icon, required this.children});
  final IconData icon;
  final List<Widget> children;
  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 3),
      decoration: BoxDecoration(
        color: t.n200.withValues(alpha: 0.6),
        borderRadius: BorderRadius.circular(Radii.sm),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 12, color: t.n600),
          const SizedBox(width: 5),
          ...children,
        ],
      ),
    );
  }
}

class _Chip extends ConsumerWidget {
  const _Chip({super.key, required this.receipt});
  final MemoryReceipt receipt;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final kept =
        receipt.kind == MemoryReceiptKind.alreadyRemembered ||
        receipt.kind == MemoryReceiptKind.updated;
    final label = switch (receipt.kind) {
      MemoryReceiptKind.updated => i18n.t(
        'chat:assistant.memory.updated',
        vars: {'summary': receipt.summary},
      ),
      MemoryReceiptKind.alreadyRemembered => i18n.t(
        'chat:assistant.memory.alreadyRemembered',
      ),
      MemoryReceiptKind.refused => i18n.t('chat:assistant.memory.refused'),
      MemoryReceiptKind.paused => i18n.t('chat:assistant.memory.paused'),
      _ => i18n.t('chat:assistant.memory.forgotten'),
    };
    return _ChipShell(
      icon: kept
          ? Icons.bookmark_added_outlined
          : Icons.bookmark_remove_outlined,
      children: [
        Flexible(
          child: Text(
            label,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
          ),
        ),
      ],
    );
  }
}

class _RememberedChip extends ConsumerStatefulWidget {
  const _RememberedChip({
    super.key,
    required this.scope,
    required this.partId,
    required this.receipt,
  });
  final AssistantScope scope;
  final String partId;
  final MemoryReceipt receipt;
  @override
  ConsumerState<_RememberedChip> createState() => _RememberedChipState();
}

class _RememberedChipState extends ConsumerState<_RememberedChip> {
  String _state = 'kept';

  Future<void> _undo() async {
    if (_state != 'kept') return;
    setState(() => _state = 'undoing');
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    try {
      // One request id per receipt: a retry after a lost response is a no-op.
      await ref
          .read(assistantApiProvider(widget.scope))
          .forgetMemory(
            widget.receipt.memoryId!,
            revision: widget.receipt.revision,
            requestId: 'assistant-undo:${widget.partId}',
          );
      if (mounted) setState(() => _state = 'undone');
    } catch (error) {
      if (!mounted) return;
      setState(() => _state = 'kept');
      toast.error(
        apiErrorOf(error)?.code == 'MEMORY_REVISION_CONFLICT'
            ? i18n.t('chat:assistant.memory.undoChanged')
            : errorText(i18n, error),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final undone = _state == 'undone';
    return _ChipShell(
      icon: undone
          ? Icons.bookmark_remove_outlined
          : Icons.bookmark_added_outlined,
      children: [
        Flexible(
          child: Text(
            i18n.t(
              'chat:assistant.memory.remembered',
              vars: {'summary': widget.receipt.summary},
            ),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              fontSize: FontSizes.xs,
              color: t.n600,
              decoration: undone ? TextDecoration.lineThrough : null,
            ),
          ),
        ),
        Text(
          ' · ',
          style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
        ),
        GestureDetector(
          onTap: _state == 'kept' ? _undo : null,
          child: Text(
            i18n.t(switch (_state) {
              'undone' => 'chat:assistant.memory.undone',
              'undoing' => 'chat:assistant.memory.undoing',
              _ => 'chat:assistant.memory.undo',
            }),
            style: TextStyle(
              fontSize: FontSizes.xs,
              color: _state == 'kept' ? t.ink : t.n600,
              decoration: _state == 'kept' ? TextDecoration.underline : null,
            ),
          ),
        ),
      ],
    );
  }
}
