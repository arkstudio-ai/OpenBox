import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/session.dart';

/// What a turn shows before its first words arrive (web `ThinkingRow`).
///
/// This was a shimmering line, which promises text that is about to appear.
/// When a run stalls — an upstream account needing re-auth, five retries
/// across a minute — that shimmer keeps promising, and the wait reads as the
/// app having hung rather than as work still in progress. Saying what is
/// happening costs one line and removes the ambiguity.
class ThinkingRow extends ConsumerStatefulWidget {
  const ThinkingRow({super.key, this.retry});

  /// Present only while retrying.
  final RetryProgress? retry;

  @override
  ConsumerState<ThinkingRow> createState() => _ThinkingRowState();
}

class _ThinkingRowState extends ConsumerState<ThinkingRow>
    with SingleTickerProviderStateMixin {
  late final AnimationController _anim = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1100),
  )..repeat();

  @override
  void dispose() {
    _anim.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final retry = widget.retry;
    final retrying = retry != null && retry.attempt > 0;
    final tone = retrying ? t.sage : t.n600;
    final label = retrying
        ? i18n.t('chat:status.retrying', vars: {
            'attempt': retry.attempt,
            'total': retry.maxAttempts > 0 ? retry.maxAttempts : retry.attempt,
          })
        : i18n.t('chat:status.thinking');

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          for (var i = 0; i < 3; i += 1)
            Padding(
              padding: const EdgeInsets.only(right: 4),
              // Staggered so the three read as one travelling pulse rather
              // than three things blinking together.
              child: _PulseDot(anim: _anim, delay: i * 0.16, color: tone),
            ),
          const SizedBox(width: 4),
          Text(label, style: TextStyle(fontSize: FontSizes.md, color: tone)),
        ],
      ),
    );
  }
}

class _PulseDot extends AnimatedWidget {
  const _PulseDot({
    required Animation<double> anim,
    required this.delay,
    required this.color,
  }) : super(listenable: anim);

  final double delay;
  final Color color;

  @override
  Widget build(BuildContext context) {
    final anim = listenable as Animation<double>;
    final phase = (anim.value + delay) % 1.0;
    // 0.35 → 1 → 0.35 opacity, 0.82 → 1 → 0.82 scale (web `pulse-dot`).
    final wave = phase < 0.5 ? phase * 2 : (1 - phase) * 2;
    final size = 6.0 * (0.82 + 0.18 * wave);
    return SizedBox(
      width: 6,
      height: 6,
      child: Center(
        child: Container(
          width: size,
          height: size,
          decoration: BoxDecoration(
            color: color.withValues(alpha: 0.35 + 0.65 * wave),
            shape: BoxShape.circle,
          ),
        ),
      ),
    );
  }
}

/// Placeholder before the assistant's first part arrives (web `TypingRow`).
class TypingRow extends StatelessWidget {
  const TypingRow({super.key, this.retry});

  final RetryProgress? retry;

  @override
  Widget build(BuildContext context) => Align(
        alignment: Alignment.centerLeft,
        child: ThinkingRow(retry: retry),
      );
}
