import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';

/// Failed-turn card (web `InlineErrorCard`): message + Regenerate + Dismiss.
class InlineErrorCard extends ConsumerWidget {
  const InlineErrorCard({
    super.key,
    required this.message,
    required this.onRegenerate,
    required this.onDismiss,
  });

  final String message;
  final VoidCallback onRegenerate;
  final VoidCallback onDismiss;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 8),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: t.dangerSoft,
        borderRadius: BorderRadius.circular(Radii.xl),
        border: Border.all(color: t.danger.withValues(alpha: 0.25)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            i18n.t('chat:meta.errorTitle'),
            style: TextStyle(
              fontSize: FontSizes.sm,
              fontWeight: FontWeight.w600,
              color: t.danger,
            ),
          ),
          if (message.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              message,
              style: TextStyle(
                  fontSize: FontSizes.sm, color: t.dangerInk, height: 1.5),
            ),
          ],
          const SizedBox(height: 10),
          Row(
            children: [
              OutlinedButton(
                onPressed: onRegenerate,
                style: OutlinedButton.styleFrom(
                  side: BorderSide(color: t.hair),
                  foregroundColor: t.ink,
                  padding:
                      const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                ),
                child: Text(i18n.t('chat:meta.regenerate'),
                    style: const TextStyle(fontSize: FontSizes.sm)),
              ),
              const SizedBox(width: 8),
              TextButton(
                onPressed: onDismiss,
                child: Text(
                  i18n.t('chat:meta.dismissTurn'),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
