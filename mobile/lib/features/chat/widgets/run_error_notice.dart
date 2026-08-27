import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';

/// Why the last run produced nothing, kept above the composer
/// (web `RunErrorNotice`).
///
/// The toast is easy to miss and easy to dismiss, and once it is gone a
/// failed turn looks exactly like a successful one that happened to be quiet
/// — which reads as the app being broken rather than the request having
/// failed. This line stays until the next message is sent, so there is always
/// something to find. It lives outside the scroll area for the same reason
/// the question dock does: it must not be scrollable away.
class RunErrorNotice extends ConsumerWidget {
  const RunErrorNotice({
    super.key,
    required this.message,
    required this.onDismiss,
  });

  final String message;
  final VoidCallback onDismiss;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 0, 10, 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: 2),
            child: Icon(Icons.error_outline, size: 13, color: t.danger),
          ),
          const SizedBox(width: 6),
          Expanded(
            child: Text(
              message,
              style: TextStyle(
                fontSize: FontSizes.xs,
                height: 1.55,
                color: t.danger,
              ),
            ),
          ),
          IconButton(
            onPressed: onDismiss,
            icon: Icon(Icons.close, size: 12, color: t.danger),
            tooltip: i18n.t('common:close'),
            visualDensity: VisualDensity.compact,
            constraints: const BoxConstraints.tightFor(width: 24, height: 24),
            padding: EdgeInsets.zero,
          ),
        ],
      ),
    );
  }
}
