import 'package:flutter/material.dart';

import '../appearance/tokens.dart';
import '../appearance/type_scale.dart';

/// One control in the composer's toolbar row (web: the pills beside the model
/// picker). Shared so controls injected by the app layer — the team picker —
/// sit on the same line as the chat feature's own pills instead of growing a
/// second row in a different visual language.
class ComposerPill extends StatelessWidget {
  const ComposerPill({
    super.key,
    required this.label,
    required this.icon,
    required this.onTap,
    this.maxWidth = 110,
  });

  final String label;
  final IconData icon;

  /// Null reads as unavailable rather than absent: the pill stays in place at
  /// reduced contrast while a turn is running.
  final VoidCallback? onTap;
  final double maxWidth;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Opacity(
      opacity: onTap == null ? 0.5 : 1,
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          borderRadius: BorderRadius.circular(Radii.full),
          onTap: onTap,
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
            decoration: BoxDecoration(
              border: Border.all(color: t.hair),
              borderRadius: BorderRadius.circular(Radii.full),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(icon, size: 13, color: t.n600),
                const SizedBox(width: 5),
                ConstrainedBox(
                  constraints: BoxConstraints(maxWidth: maxWidth),
                  child: Text(
                    label,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
                  ),
                ),
                const SizedBox(width: 2),
                Icon(Icons.expand_more, size: 13, color: t.n500),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
