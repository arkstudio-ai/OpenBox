import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';

/// A rule across the transcript where a turn was cut short (web
/// `InterruptionDivider`).
///
/// The interruption marker is a real message — that is the point, the next
/// turn's model has to read it in-band — but it is not something a person
/// said. Without this it renders as an *empty* bubble, because UserBubble
/// drops synthetic text, which reads as the app having lost a message.
class InterruptionDivider extends ConsumerStatefulWidget {
  const InterruptionDivider({super.key, required this.message});

  final ChatMessage message;

  @override
  ConsumerState<InterruptionDivider> createState() =>
      _InterruptionDividerState();
}

class _InterruptionDividerState extends ConsumerState<InterruptionDivider> {
  bool _open = false;

  String get _detail => widget.message.parts
      .whereType<TextPart>()
      .map((TextPart p) => p.text)
      .join('\n')
      .trim();

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final detail = _detail;

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(child: Divider(color: t.hair, height: 1)),
              const SizedBox(width: 12),
              InkWell(
                onTap: detail.isEmpty
                    ? null
                    : () => setState(() => _open = !_open),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.stop, size: 12, color: t.n500),
                    const SizedBox(width: 6),
                    Text(
                      i18n.t('chat:interrupted.label'),
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
                    ),
                    if (detail.isNotEmpty)
                      Icon(
                        _open ? Icons.expand_less : Icons.expand_more,
                        size: 14,
                        color: t.n500,
                      ),
                  ],
                ),
              ),
              const SizedBox(width: 12),
              Expanded(child: Divider(color: t.hair, height: 1)),
            ],
          ),
          if (_open && detail.isNotEmpty)
            Container(
              margin: const EdgeInsets.only(top: 8),
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                border: Border.all(color: t.hair),
                borderRadius: BorderRadius.circular(8),
              ),
              child: Text(
                detail,
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
            ),
        ],
      ),
    );
  }
}
