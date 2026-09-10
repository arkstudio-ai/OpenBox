import 'dart:async';

import 'package:flutter/material.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';

/// Three equal-width placeholders align with the input and completed choices.
/// The server deadline retires a persisted wait even after an app restart.
class SuggestionLoading extends StatefulWidget {
  const SuggestionLoading({
    super.key,
    required this.expiresAt,
    required this.label,
  });

  final DateTime? expiresAt;
  final String label;

  @override
  State<SuggestionLoading> createState() => _SuggestionLoadingState();
}

class _SuggestionLoadingState extends State<SuggestionLoading>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller;
  Timer? _deadline;
  bool _waiting = false;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1800),
    );
    _syncDeadline();
  }

  void _syncDeadline() {
    _deadline?.cancel();
    final remaining = widget.expiresAt?.difference(DateTime.now());
    _waiting = remaining != null && remaining > Duration.zero;
    if (!_waiting) return;
    final bounded = remaining! > const Duration(seconds: 60)
        ? const Duration(seconds: 60)
        : remaining;
    _deadline = Timer(bounded, () {
      _controller.stop();
      setState(() => _waiting = false);
    });
  }

  void _syncAnimation() {
    if (_waiting && !MediaQuery.disableAnimationsOf(context)) {
      if (!_controller.isAnimating) _controller.repeat();
    } else {
      _controller.stop();
    }
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _syncAnimation();
  }

  @override
  void didUpdateWidget(SuggestionLoading oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.expiresAt != widget.expiresAt) _syncDeadline();
    _syncAnimation();
  }

  @override
  void dispose() {
    _deadline?.cancel();
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (!_waiting) return const SizedBox.shrink();
    final t = context.tokens;
    final animate = !MediaQuery.disableAnimationsOf(context);
    return Semantics(
      container: true,
      liveRegion: true,
      label: widget.label,
      child: ExcludeSemantics(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 2),
          child: AnimatedBuilder(
            animation: _controller,
            builder: (context, _) => Row(
              children: [
                for (var index = 0; index < 3; index++) ...[
                  if (index > 0) const SizedBox(width: 8),
                  Expanded(
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(Radii.lg),
                      child: Container(
                        height:
                            MediaQuery.textScalerOf(
                                  context,
                                ).scale(FontSizes.sm) *
                                2.8 +
                            16,
                        decoration: BoxDecoration(
                          color: t.hairSoft,
                          border: Border.all(color: t.hair),
                          borderRadius: BorderRadius.circular(Radii.lg),
                        ),
                        child: !animate
                            ? null
                            : FractionalTranslation(
                                translation: Offset(
                                  Curves.easeInOut.transform(
                                            _controller.value,
                                          ) *
                                          2 -
                                      1,
                                  0,
                                ),
                                child: DecoratedBox(
                                  decoration: BoxDecoration(
                                    gradient: LinearGradient(
                                      begin: Alignment.topLeft,
                                      end: Alignment.bottomRight,
                                      colors: [
                                        Colors.transparent,
                                        t.n500.withValues(alpha: 0.24),
                                        Colors.transparent,
                                      ],
                                    ),
                                  ),
                                ),
                              ),
                      ),
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}
