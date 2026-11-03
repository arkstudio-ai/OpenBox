import 'package:flutter/widgets.dart';

import '../utils/content_view.dart';

/// What a Light topic page puts under a finished result (a video or an
/// image the person asked for): save, publish, make another, keep doing it.
typedef LightResultActionsBuilder =
    Widget Function(BuildContext context, ArtifactGroup group);

/// Marks a transcript as shown on a Light 模式 topic page (docs
/// `LIGHT_MODE_PLAN.md` §4.4): tool traces fold to one plain-words line,
/// question cards wear the big-chip skin, and result cards carry the page's
/// own actions. Everything else renders as in the pro chat — the
/// conversation itself is the same.
class LightChatScope extends InheritedWidget {
  const LightChatScope({super.key, required super.child, this.resultActions});

  /// Supplied by the Light feature (chat never imports it).
  final LightResultActionsBuilder? resultActions;

  static bool isLight(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<LightChatScope>() != null;

  static LightChatScope? of(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<LightChatScope>();

  @override
  bool updateShouldNotify(LightChatScope oldWidget) =>
      resultActions != oldWidget.resultActions;
}
