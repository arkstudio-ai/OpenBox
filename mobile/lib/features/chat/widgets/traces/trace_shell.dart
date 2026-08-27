import 'package:flutter/material.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/widgets/fold.dart';
import '../../../../shared/widgets/shimmer_text.dart';

/// Latched accordion (web `TraceShell.tsx`): opens when its phase goes
/// live, auto-closes ONCE when the turn starts producing prose, otherwise
/// holds state including the user's manual toggle — no per-frame derivation
/// (prevents flicker, web D.4.4).
class TraceShell extends StatefulWidget {
  const TraceShell({
    super.key,
    required this.title,
    required this.active,
    required this.autoCollapseReady,
    required this.child,
    this.summary,
    this.defaultOpen = false,
  });

  final String title;
  final String? summary;

  /// This phase is currently live (streams shimmer on the title).
  final bool active;

  /// The turn has begun producing its answer → collapse once.
  final bool autoCollapseReady;

  /// Completed-but-incomplete work should reopen on transcript reload.
  final bool defaultOpen;

  final Widget child;

  @override
  State<TraceShell> createState() => _TraceShellState();
}

class _TraceShellState extends State<TraceShell> {
  late bool _open = widget.active || widget.defaultOpen;
  bool _autoCollapsed = false;

  @override
  void didUpdateWidget(TraceShell oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.active && !oldWidget.active) {
      _open = true;
    }
    if (widget.autoCollapseReady && !oldWidget.autoCollapseReady && !_autoCollapsed) {
      _open = false;
      _autoCollapsed = true;
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final titleStyle = TextStyle(
      fontSize: FontSizes.sm,
      fontWeight: FontWeight.w500,
      color: t.n600,
    );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        InkWell(
          borderRadius: BorderRadius.circular(Radii.sm),
          onTap: () => setState(() => _open = !_open),
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 6),
            child: Row(
              children: [
                ShimmerText(widget.title,
                    style: titleStyle, enabled: widget.active),
                if (widget.summary != null) ...[
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      widget.summary!,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
                    ),
                  ),
                ] else
                  const Spacer(),
                AnimatedRotation(
                  turns: _open ? 0.5 : 0,
                  duration: const Duration(milliseconds: 180),
                  child: Icon(Icons.expand_more, size: 16, color: t.n500),
                ),
              ],
            ),
          ),
        ),
        Fold(
          open: _open,
          child: Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: widget.child,
          ),
        ),
      ],
    );
  }
}
