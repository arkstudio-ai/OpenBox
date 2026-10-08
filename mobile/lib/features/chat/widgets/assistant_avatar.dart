import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';

/// The personal assistant's face: one small mark, the same everywhere it
/// speaks (web `AssistantAvatar`).
class AssistantAvatar extends StatelessWidget {
  const AssistantAvatar({super.key, this.large = false});

  final bool large;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final size = large ? 56.0 : 24.0;
    return ExcludeSemantics(
      child: Container(
        width: size,
        height: size,
        decoration: BoxDecoration(color: t.a200, shape: BoxShape.circle),
        alignment: Alignment.center,
        child: Icon(Icons.auto_awesome, size: large ? 26 : 13, color: t.accent),
      ),
    );
  }
}
