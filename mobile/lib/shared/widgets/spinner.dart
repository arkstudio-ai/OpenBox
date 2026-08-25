import 'package:flutter/material.dart';

import '../appearance/tokens.dart';

/// Small arc spinner (web `--animate-spin-arc`, 0.9s linear).
class Spinner extends StatelessWidget {
  const Spinner({super.key, this.size = 14, this.color});

  final double size;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: size,
      height: size,
      child: CircularProgressIndicator(
        strokeWidth: 1.6,
        color: color ?? context.tokens.n600,
      ),
    );
  }
}

/// Pulsing dot (web `--animate-pulse-dot`, 1.1s ease-in-out: opacity
/// 0.35↔1, scale 0.82↔1). Used on running tool timeline dots.
class PulseDot extends StatefulWidget {
  const PulseDot({super.key, required this.color, this.size = 7});

  final Color color;
  final double size;

  @override
  State<PulseDot> createState() => _PulseDotState();
}

class _PulseDotState extends State<PulseDot> with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1100),
  )..repeat(reverse: true);

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return FadeTransition(
      opacity: Tween(begin: 0.35, end: 1.0)
          .animate(CurvedAnimation(parent: _controller, curve: Curves.easeInOut)),
      child: ScaleTransition(
        scale: Tween(begin: 0.82, end: 1.0).animate(
          CurvedAnimation(parent: _controller, curve: Curves.easeInOut),
        ),
        child: Container(
          width: widget.size,
          height: widget.size,
          decoration: BoxDecoration(color: widget.color, shape: BoxShape.circle),
        ),
      ),
    );
  }
}
