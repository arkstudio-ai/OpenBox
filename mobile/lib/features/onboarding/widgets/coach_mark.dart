import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../state/onboarding_store.dart';

/// Marks a widget as a coach-mark target. The key is stable across rebuilds,
/// so a sequence can find the widget later by name.
class CoachAnchor extends StatelessWidget {
  const CoachAnchor({super.key, required this.name, required this.child});

  final String name;
  final Widget child;

  // One key instance per name: GlobalObjectKey compares by identity, so a
  // name built at runtime would never find the mounted anchor.
  static final Map<String, GlobalKey> _keys = {};

  static GlobalKey keyFor(String name) =>
      _keys.putIfAbsent(name, () => GlobalKey(debugLabel: 'coach:$name'));

  /// Screen rect of the anchor, or null when it is not laid out.
  static Rect? rectOf(String name) {
    final context = keyFor(name).currentContext;
    final box = context?.findRenderObject();
    if (box is! RenderBox || !box.hasSize || !box.attached) return null;
    final origin = box.localToGlobal(Offset.zero);
    return origin & box.size;
  }

  @override
  Widget build(BuildContext context) =>
      KeyedSubtree(key: keyFor(name), child: child);
}

class CoachStep {
  const CoachStep({
    required this.anchor,
    required this.title,
    required this.body,
    this.radius = 999,
    this.padding = 4,
  });

  final String anchor;
  final String title;
  final String body;
  final double radius;
  final double padding;
}

/// Shows [steps] as a masked walkthrough above the current route, claiming
/// the guide queue for [guideKey] and marking it seen on finish or skip.
/// Returns false when another guide is showing or [guideKey] was seen.
Future<bool> showCoachMarks(
  BuildContext context,
  WidgetRef ref, {
  required String guideKey,
  required List<CoachStep> steps,
}) async {
  final onboarding = ref.read(onboardingProvider.notifier);
  if (!onboarding.shouldShow(guideKey)) return false;
  final live = steps.where((s) => CoachAnchor.rectOf(s.anchor) != null).toList();
  if (live.isEmpty) return false;
  final queue = ref.read(guideQueueProvider.notifier);
  if (!queue.claim(guideKey)) return false;
  // Mark first: an interrupted walkthrough (back gesture, route change)
  // must not come back on the next visit.
  unawaited(onboarding.markSeen(guideKey));
  try {
    await Navigator.of(context, rootNavigator: true).push<void>(
      PageRouteBuilder<void>(
        opaque: false,
        barrierDismissible: false,
        transitionDuration: const Duration(milliseconds: 160),
        reverseTransitionDuration: const Duration(milliseconds: 120),
        pageBuilder: (_, animation, _) => FadeTransition(
          opacity: animation,
          child: _CoachMarkRoute(steps: live),
        ),
      ),
    );
  } finally {
    queue.release(guideKey);
  }
  return true;
}

class _CoachMarkRoute extends ConsumerStatefulWidget {
  const _CoachMarkRoute({required this.steps});

  final List<CoachStep> steps;

  @override
  ConsumerState<_CoachMarkRoute> createState() => _CoachMarkRouteState();
}

class _CoachMarkRouteState extends ConsumerState<_CoachMarkRoute> {
  int _index = 0;

  CoachStep get _step => widget.steps[_index];

  void _next() {
    if (_index + 1 >= widget.steps.length) {
      Navigator.of(context).pop();
    } else {
      setState(() => _index++);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final rect = CoachAnchor.rectOf(_step.anchor);
    if (rect == null) {
      // The anchor left the tree mid-sequence: end quietly.
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) Navigator.of(context).pop();
      });
      return const SizedBox.shrink();
    }
    final hole = rect.inflate(_step.padding);
    final size = MediaQuery.sizeOf(context);
    final below = hole.bottom + 12;
    final placeBelow = below + 170 < size.height || hole.top < 200;
    final last = _index + 1 == widget.steps.length;
    return Material(
      type: MaterialType.transparency,
      child: Stack(
        children: [
          Positioned.fill(
            child: CustomPaint(
              painter: _MaskPainter(hole: hole, radius: _step.radius),
            ),
          ),
          Positioned(
            left: 20,
            right: 20,
            top: placeBelow ? below : null,
            bottom: placeBelow ? null : size.height - hole.top + 12,
            child: _Bubble(
              title: _step.title,
              body: _step.body,
              counter: widget.steps.length > 1
                  ? i18n.t(
                      'onboarding:marks.step',
                      vars: {
                        'current': _index + 1,
                        'total': widget.steps.length,
                      },
                    )
                  : null,
              arrowUp: placeBelow,
              arrowX: (hole.center.dx - 20 - 7).clamp(12.0, size.width - 66),
              primary: i18n.t(
                last ? 'onboarding:marks.done' : 'onboarding:marks.next',
              ),
              secondary: widget.steps.length > 1 && !last
                  ? i18n.t('onboarding:marks.skipAll')
                  : null,
              onPrimary: _next,
              onSecondary: () => Navigator.of(context).pop(),
              tokens: t,
            ),
          ),
        ],
      ),
    );
  }
}

class _MaskPainter extends CustomPainter {
  _MaskPainter({required this.hole, required this.radius});

  final Rect hole;
  final double radius;

  @override
  void paint(Canvas canvas, Size size) {
    final path = Path()
      ..addRect(Offset.zero & size)
      ..addRRect(RRect.fromRectAndRadius(hole, Radius.circular(radius)))
      ..fillType = PathFillType.evenOdd;
    canvas.drawPath(path, Paint()..color = const Color(0x94262520));
    // A faint ring makes the cutout read on the light rail as well.
    canvas.drawRRect(
      RRect.fromRectAndRadius(hole, Radius.circular(radius)),
      Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.5
        ..color = const Color(0xCCFFFFFF),
    );
  }

  @override
  bool shouldRepaint(_MaskPainter old) =>
      old.hole != hole || old.radius != radius;
}

class _Bubble extends StatelessWidget {
  const _Bubble({
    required this.title,
    required this.body,
    required this.counter,
    required this.arrowUp,
    required this.arrowX,
    required this.primary,
    required this.secondary,
    required this.onPrimary,
    required this.onSecondary,
    required this.tokens,
  });

  final String title;
  final String body;
  final String? counter;
  final bool arrowUp;
  final double arrowX;
  final String primary;
  final String? secondary;
  final VoidCallback onPrimary;
  final VoidCallback onSecondary;
  final BossipTokens tokens;

  @override
  Widget build(BuildContext context) {
    final t = tokens;
    final card = Container(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 14),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.lg),
        boxShadow: const [
          BoxShadow(
            color: Color(0x38000000),
            blurRadius: 32,
            offset: Offset(0, 12),
          ),
        ],
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.baseline,
            textBaseline: TextBaseline.alphabetic,
            children: [
              Expanded(
                child: Text(
                  title,
                  style: TextStyle(
                    fontSize: FontSizes.lg,
                    fontWeight: FontWeight.w600,
                    color: t.ink,
                  ),
                ),
              ),
              if (counter != null)
                Text(
                  counter!,
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
            ],
          ),
          const SizedBox(height: 6),
          Text(
            body,
            style: TextStyle(
              fontSize: FontSizes.md,
              height: 1.6,
              color: t.n800,
            ),
          ),
          const SizedBox(height: 12),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              if (secondary != null)
                TextButton(
                  onPressed: onSecondary,
                  child: Text(
                    secondary!,
                    style: TextStyle(fontSize: FontSizes.md, color: t.n600),
                  ),
                )
              else
                const SizedBox.shrink(),
              FilledButton(
                onPressed: onPrimary,
                style: FilledButton.styleFrom(
                  backgroundColor: t.a700,
                  foregroundColor: t.bg,
                  minimumSize: const Size(0, 36),
                  padding: const EdgeInsets.symmetric(horizontal: 18),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                ),
                child: Text(
                  primary,
                  style: const TextStyle(
                    fontSize: FontSizes.md,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ],
          ),
        ],
      ),
    );
    final arrow = Padding(
      padding: EdgeInsets.only(left: arrowX),
      child: Transform.rotate(
        angle: 0.785398,
        child: Container(
          width: 14,
          height: 14,
          decoration: BoxDecoration(
            color: t.card,
            borderRadius: BorderRadius.circular(2),
          ),
        ),
      ),
    );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        if (arrowUp) Transform.translate(offset: const Offset(0, 7), child: arrow),
        card,
        if (!arrowUp)
          Transform.translate(offset: const Offset(0, -7), child: arrow),
      ],
    );
  }
}
