import 'package:flutter/widgets.dart';

/// Marks a transcript as shown on a Light 模式 topic page (docs
/// `LIGHT_MODE_PLAN.md` §4.4): tool traces fold to one plain-words line and
/// question cards wear the big-chip skin. Everything else renders as in the
/// pro chat — the conversation itself is the same.
class LightChatScope extends InheritedWidget {
  const LightChatScope({super.key, required super.child});

  static bool isLight(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<LightChatScope>() != null;

  @override
  bool updateShouldNotify(LightChatScope oldWidget) => false;
}
