import 'package:flutter/material.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';

/// The 「助理」 tab (docs `LIGHT_MODE_PLAN.md` §4.7 #3): the personal
/// assistant alone. Light 模式 hands everything to the assistant; the
/// conversations the home entries start are reached from 首页「最近做的」,
/// so there is no second list here (2026-11-04, replacing the 助理 / 全部对话
/// segments). The assistant pane is built by the app layer (it needs the
/// scope, the voice button and the task sheet) and handed in.
class LightAssistantTab extends StatelessWidget {
  const LightAssistantTab({
    super.key,
    required this.title,
    required this.assistant,
    this.actions = const [],
  });

  final String title;
  final Widget assistant;
  final List<Widget> actions;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        automaticallyImplyLeading: false,
        titleSpacing: 16,
        title: Text(
          title,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
        actions: [...actions, const SizedBox(width: 4)],
      ),
      body: assistant,
    );
  }
}
