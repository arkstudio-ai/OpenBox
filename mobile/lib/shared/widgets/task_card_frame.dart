import 'package:flutter/material.dart';

import '../appearance/tokens.dart';
import '../appearance/type_scale.dart';

/// Shared by the ordinary task list and team progress.
class TaskCardFrame extends StatelessWidget {
  const TaskCardFrame({super.key, required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) => Container(
    margin: const EdgeInsets.only(bottom: 8),
    padding: const EdgeInsets.all(14),
    decoration: BoxDecoration(
      color: context.tokens.card,
      borderRadius: BorderRadius.circular(Radii.xl),
      border: Border.all(color: context.tokens.hair),
    ),
    child: Material(type: MaterialType.transparency, child: child),
  );
}
