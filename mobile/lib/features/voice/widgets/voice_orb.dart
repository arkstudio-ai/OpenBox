import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../state/voice_call_state.dart';

/// The call's one picture: grows with the voice that is speaking (yours
/// while it listens, the assistant's while it talks), breathes slowly while
/// it thinks or works, goes grey once the call is over. Decorative only.
class VoiceOrb extends StatefulWidget {
  const VoiceOrb({
    super.key,
    required this.call,
    required this.micLevel,
    required this.outputLevel,
    this.size = 160,
  });

  final VoiceCallState call;
  final ValueListenable<double> micLevel;
  final ValueListenable<double> outputLevel;
  final double size;

  @override
  State<VoiceOrb> createState() => _VoiceOrbState();
}

class _VoiceOrbState extends State<VoiceOrb>
    with SingleTickerProviderStateMixin {
  late final AnimationController _breath = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1600),
  );

  bool get _breathing {
    final call = widget.call;
    return call.dialling ||
        (call.status == VoiceCallStatus.connected &&
            (call.phase == VoicePhase.thinking ||
                call.phase == VoicePhase.working));
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _syncBreath();
  }

  @override
  void didUpdateWidget(VoiceOrb oldWidget) {
    super.didUpdateWidget(oldWidget);
    _syncBreath();
  }

  void _syncBreath() {
    if (_breathing && !MediaQuery.of(context).disableAnimations) {
      if (!_breath.isAnimating) _breath.repeat(reverse: true);
    } else if (_breath.isAnimating || _breath.value != 0) {
      _breath
        ..stop()
        ..value = 0;
    }
  }

  @override
  void dispose() {
    _breath.dispose();
    super.dispose();
  }

  Color _color(BossipTokens t) {
    final call = widget.call;
    return switch (call.status) {
      VoiceCallStatus.connected => switch (call.phase) {
        VoicePhase.listening => call.muted ? t.n500 : t.s600,
        VoicePhase.thinking => t.n500,
        VoicePhase.greeting || VoicePhase.speaking => t.accent,
        VoicePhase.working => t.a700,
      },
      VoiceCallStatus.ended || VoiceCallStatus.idle => t.n400,
      _ => t.n500,
    };
  }

  double _scale() {
    final call = widget.call;
    if (MediaQuery.of(context).disableAnimations) return 1;
    if (_breathing) return 1 + 0.08 * Curves.easeInOut.transform(_breath.value);
    if (call.status != VoiceCallStatus.connected) return 1;
    return switch (call.phase) {
      VoicePhase.listening => call.muted ? 1 : 1 + 0.3 * widget.micLevel.value,
      VoicePhase.greeting ||
      VoicePhase.speaking => 1 + 0.3 * widget.outputLevel.value,
      _ => 1,
    };
  }

  @override
  Widget build(BuildContext context) {
    final color = _color(context.tokens);
    final size = widget.size;
    return ExcludeSemantics(
      child: SizedBox.square(
        // Room to grow without moving anything around it.
        dimension: size * 1.3,
        child: Center(
          child: AnimatedBuilder(
            animation: Listenable.merge([
              _breath,
              widget.micLevel,
              widget.outputLevel,
            ]),
            builder: (context, child) => AnimatedScale(
              scale: _scale(),
              duration: const Duration(milliseconds: 90),
              child: child,
            ),
            child: AnimatedContainer(
              duration: const Duration(milliseconds: 250),
              width: size,
              height: size,
              alignment: Alignment.center,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: color.withValues(alpha: 0.16),
              ),
              child: AnimatedContainer(
                duration: const Duration(milliseconds: 250),
                width: size * 0.64,
                height: size * 0.64,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: color,
                  boxShadow: [
                    BoxShadow(
                      color: color.withValues(alpha: 0.35),
                      blurRadius: 24,
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
