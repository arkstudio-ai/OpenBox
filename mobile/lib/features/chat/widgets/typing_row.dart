import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/widgets/shimmer_text.dart';

/// Awaiting-response row (web `TypingRow`/`StreamSkeleton`): shimmering
/// "thinking…" line shown when busy and the last turn is a user message.
class TypingRow extends ConsumerWidget {
  const TypingRow({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    return Align(
      alignment: Alignment.centerLeft,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: ShimmerText(
          i18n.t('chat:thinking'),
          style: const TextStyle(fontSize: FontSizes.base),
        ),
      ),
    );
  }
}
