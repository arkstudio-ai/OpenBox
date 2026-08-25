import 'package:flutter/material.dart';

import '../../../../shared/appearance/tokens.dart';

/// Context-window usage ring (web `ContextRing`): tiny circular gauge of
/// `token_usage.context / model.context_limit`.
class ContextRing extends StatelessWidget {
  const ContextRing({super.key, required this.used, required this.limit});

  final int used;
  final int limit;

  @override
  Widget build(BuildContext context) {
    if (limit <= 0) return const SizedBox.shrink();
    final t = context.tokens;
    final pct = (used / limit).clamp(0.0, 1.0);
    final color = pct > 0.85 ? t.danger : t.a700;
    return SizedBox(
      width: 18,
      height: 18,
      child: CircularProgressIndicator(
        value: pct,
        strokeWidth: 2.4,
        color: color,
        backgroundColor: t.n300,
      ),
    );
  }
}
