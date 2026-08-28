import 'package:flutter/material.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/widgets/fold.dart';
import '../../../../shared/widgets/shimmer_text.dart';

/// Collapsed trace row (web `TraceShell.tsx`).
///
/// Open/closed belongs to the reader, and to nobody else. This used to open
/// itself whenever the phase went live and close itself when the turn started
/// answering. Both flags flip repeatedly within one turn — reasoning
/// alternates with tool calls, and a tool chain goes quiet between two calls
/// — so every flip re-opened a row the reader had just collapsed, and a
/// streaming turn kept several hundred pixels of trace open by default. Now
/// nothing but the toggle moves it.
///
/// Collapsed is not silent: the title shimmers while the phase runs and the
/// summary carries the live count or the call in flight, so the row still
/// says what is happening — in one line instead of a column.
class TraceShell extends StatefulWidget {
  const TraceShell({
    super.key,
    required this.title,
    required this.active,
    required this.child,
    this.summary,
    this.defaultOpen = false,
  });

  final String title;
  final String? summary;

  /// This phase is currently live (shimmers the title).
  final bool active;

  /// Start open. Only for a finished turn that owes an explanation.
  final bool defaultOpen;

  final Widget child;

  @override
  State<TraceShell> createState() => _TraceShellState();
}

class _TraceShellState extends State<TraceShell> {
  late bool _open = widget.defaultOpen;

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
              crossAxisAlignment: CrossAxisAlignment.center,
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
                      style: TextStyle(fontSize: FontSizes.xs2, color: t.n500),
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
