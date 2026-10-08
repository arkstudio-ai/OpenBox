import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../api/assistant_api.dart';

/// "参考了 3 条记忆" under a reply of the personal assistant (web
/// `RecalledMemories`): the memories that came up for the message it
/// answered. A tap lists them, with where to fix what is wrong.
class RecalledMemories extends ConsumerStatefulWidget {
  const RecalledMemories({
    super.key,
    required this.scope,
    required this.sessionId,
    required this.messageId,
    required this.streaming,
  });

  final AssistantScope scope;
  final String sessionId;

  /// The user's message the reply answered.
  final String? messageId;
  final bool streaming;

  @override
  ConsumerState<RecalledMemories> createState() => _RecalledMemoriesState();
}

class _RecalledMemoriesState extends ConsumerState<RecalledMemories> {
  bool _open = false;

  ({AssistantScope scope, String sessionId}) get _key =>
      (scope: widget.scope, sessionId: widget.sessionId);

  @override
  void didUpdateWidget(RecalledMemories oldWidget) {
    super.didUpdateWidget(oldWidget);
    // Recall is recorded as the reply starts; read again once it is done, so
    // a new reply shows its own.
    if (oldWidget.streaming && !widget.streaming) {
      ref.invalidate(recalledMemoriesProvider(_key));
    }
  }

  @override
  Widget build(BuildContext context) {
    final messageId = widget.messageId;
    if (widget.streaming || messageId == null) return const SizedBox.shrink();
    final memories =
        ref.watch(recalledMemoriesProvider(_key)).valueOrNull?[messageId] ??
        const <RecalledMemory>[];
    if (memories.isEmpty) return const SizedBox.shrink();
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Semantics(
            button: true,
            expanded: _open,
            child: InkWell(
              key: const ValueKey('recalled-memories'),
              onTap: () => setState(() => _open = !_open),
              borderRadius: BorderRadius.circular(Radii.sm),
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 3),
                decoration: BoxDecoration(
                  color: t.n200.withValues(alpha: 0.4),
                  borderRadius: BorderRadius.circular(Radii.sm),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.history_rounded, size: 12, color: t.n600),
                    const SizedBox(width: 5),
                    Text(
                      i18n.t(
                        'chat:assistant.recalled.label',
                        count: memories.length,
                      ),
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                    ),
                    Icon(
                      _open ? Icons.expand_less : Icons.expand_more,
                      size: 14,
                      color: t.n600,
                    ),
                  ],
                ),
              ),
            ),
          ),
          if (_open)
            Container(
              margin: const EdgeInsets.only(top: 6),
              padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
              decoration: BoxDecoration(
                color: t.card,
                border: Border.all(color: t.hair),
                borderRadius: BorderRadius.circular(Radii.md),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    i18n.t('chat:assistant.recalled.title'),
                    style: TextStyle(
                      fontSize: FontSizes.xs,
                      fontWeight: FontWeight.w500,
                      color: t.n700,
                    ),
                  ),
                  const SizedBox(height: 6),
                  for (final memory in memories)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 4),
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text('· ', style: TextStyle(color: t.n500)),
                          Expanded(
                            child: Text(
                              memory.summary,
                              style: TextStyle(
                                fontSize: FontSizes.sm,
                                height: 1.5,
                                color: t.ink,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  const SizedBox(height: 4),
                  Text(
                    i18n.t('chat:assistant.recalled.hint'),
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                  ),
                  TextButton(
                    key: const ValueKey('recalled-manage'),
                    style: TextButton.styleFrom(
                      padding: EdgeInsets.zero,
                      minimumSize: const Size(0, 32),
                      tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                    ),
                    onPressed: () => context.push(Paths.wiki(view: 'memories')),
                    child: Text(
                      i18n.t('chat:assistant.recalled.manage'),
                      style: TextStyle(fontSize: FontSizes.xs, color: t.ink),
                    ),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}
