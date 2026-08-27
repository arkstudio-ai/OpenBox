import 'package:flutter/material.dart';

import '../appearance/tokens.dart';

/// Port of the web `.text-shimmer` running-state sweep: a clipped-text
/// gradient `n600 → ink → n600` sliding across, 2s linear infinite.
/// Honors reduced motion by falling back to solid n600.
class ShimmerText extends StatefulWidget {
  const ShimmerText(
    this.text, {
    super.key,
    this.style,
    this.enabled = true,
    this.maxLines,
  });

  final String text;
  final TextStyle? style;

  /// When false renders static n600 text (done state / reduced motion).
  final bool enabled;

  /// Clamp + ellipsis, for the rows that mirror a web `truncate`.
  final int? maxLines;

  @override
  State<ShimmerText> createState() => _ShimmerTextState();
}

class _ShimmerTextState extends State<ShimmerText>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(seconds: 2),
  );

  @override
  void initState() {
    super.initState();
    if (widget.enabled) _controller.repeat();
  }

  @override
  void didUpdateWidget(ShimmerText oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.enabled && !_controller.isAnimating) {
      _controller.repeat();
    } else if (!widget.enabled && _controller.isAnimating) {
      _controller.stop();
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final style = widget.style ?? DefaultTextStyle.of(context).style;
    final reduceMotion = MediaQuery.of(context).disableAnimations;
    if (!widget.enabled || reduceMotion) {
      return Text(
        widget.text,
        maxLines: widget.maxLines,
        overflow: widget.maxLines == null ? null : TextOverflow.ellipsis,
        style: style.copyWith(color: t.n600),
      );
    }
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, _) {
        return ShaderMask(
          blendMode: BlendMode.srcIn,
          shaderCallback: (bounds) {
            // background-position sweeps 220% → -120% over the cycle.
            final dx = (1 - _controller.value * 2.4) * bounds.width * 1.2;
            return LinearGradient(
              begin: Alignment.centerLeft,
              end: Alignment.centerRight,
              colors: [t.n600, t.n600, t.ink, t.n600, t.n600],
              stops: const [0, 0.36, 0.5, 0.64, 1],
              transform: GradientTranslation(dx),
            ).createShader(
              Rect.fromLTWH(0, 0, bounds.width * 2.2, bounds.height),
            );
          },
          child: Text(
            widget.text,
            maxLines: widget.maxLines,
            overflow: widget.maxLines == null ? null : TextOverflow.ellipsis,
            style: style.copyWith(color: Colors.white),
          ),
        );
      },
    );
  }
}

class GradientTranslation extends GradientTransform {
  const GradientTranslation(this.dx);

  final double dx;

  @override
  Matrix4 transform(Rect bounds, {TextDirection? textDirection}) =>
      Matrix4.translationValues(dx, 0, 0);
}
