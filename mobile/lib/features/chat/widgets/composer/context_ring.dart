import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/i18n/i18n.dart';

/// Context-window usage ring (web `ContextRing`): tiny circular gauge of
/// `token_usage.context / model.context_limit`.
class ContextRing extends ConsumerWidget {
  const ContextRing({
    super.key,
    required this.used,
    required this.limit,
    this.compactionThreshold,
  });

  final int used;
  final int limit;
  final int? compactionThreshold;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (limit <= 0) return const SizedBox.shrink();
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final pct = (used / limit).clamp(0.0, 1.0);
    final threshold = compactionThreshold != null && compactionThreshold! > 0
        ? compactionThreshold!.clamp(1, limit)
        : null;
    final pressure = threshold == null ? 0.0 : used / threshold;
    final color = pct >= 1 || pressure >= 1
        ? t.danger
        : pressure >= 0.875
        ? t.a700
        : t.n700;
    final percentage = used > 0 ? (pct * 100).round().clamp(1, 100) : 0;
    final detail = [
      i18n.t(
        'chat:context.used',
        vars: {'used': used, 'limit': limit, 'pct': percentage},
      ),
      if (threshold != null)
        i18n.t(
          'chat:context.compactAt',
          vars: {'tokens': threshold, 'pct': (threshold / limit * 100).round()},
        ),
      if (threshold != null && pressure >= 0.875)
        i18n.t('chat:context.compactSoon'),
    ].join('\n');
    return Tooltip(
      message: detail,
      child: Semantics(
        label: i18n.t('chat:context.aria', vars: {'pct': percentage}),
        child: SizedBox(
          width: 18,
          height: 18,
          child: CircularProgressIndicator(
            value: pct,
            strokeWidth: 2.4,
            color: color,
            backgroundColor: t.n300,
          ),
        ),
      ),
    );
  }
}
