import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart' show ScrollDirection;

import '../../../shared/appearance/tokens.dart';

/// Scrolling chat column (web `ChatFlow.tsx`): stick-to-bottom while
/// streaming (re-pin on content growth), force scroll on send, top/bottom
/// fade masks, back-to-bottom FAB, and older history put in front of the
/// transcript without moving what is on screen.
class ChatFlow extends StatefulWidget {
  const ChatFlow({
    super.key,
    required this.rows,
    this.olderCount = 0,
    this.forceScrollToken,
    this.onAtBottomChanged,
    this.controller,
    this.onNearTop,
    this.topKey,
    this.loadingOlder = false,
  });

  final List<Widget> rows;

  /// How many leading [rows] sit above the row the transcript opened on —
  /// history loaded by scrolling up. They lay out upwards from that row, so
  /// adding more moves neither what is on screen nor the bottom the list
  /// sticks to. A plain list would push everything down by their height.
  final int olderCount;

  /// Changes when the user sends → force pin + jump (web :128-141).
  final Object? forceScrollToken;
  final ValueChanged<bool>? onAtBottomChanged;
  final ScrollController? controller;

  /// The top of the transcript is close: time to fetch what precedes it.
  /// Null when there is nothing older to fetch.
  final VoidCallback? onNearTop;

  /// The first row's identity across rebuilds, such as its first message's
  /// id. [onNearTop] is called once per top row (web `ChatFlow`), not on every
  /// scroll update or streamed delta: a page that failed is asked for again
  /// when the reader leaves the top and comes back, or after a pause that
  /// doubles with each failure while they stay there.
  final Object? topKey;

  /// An older page is on its way: a small spinner at the top edge. Its end
  /// under an unchanged [topKey] means the page failed.
  final bool loadingOlder;

  @override
  State<ChatFlow> createState() => _ChatFlowState();
}

class _ChatFlowState extends State<ChatFlow> {
  /// How close to the top, in logical pixels, counts as near.
  static const _nearTop = 400.0;

  /// Scroll offset zero is the top of this sliver: the rows the transcript
  /// opened on and everything after them.
  static const _newestKey = ValueKey<String>('chat-flow-newest');

  /// [_olderAskedFor] when nothing was asked for since the reader was last
  /// away from the top.
  static const _notAsked = Object();

  /// The pause before a failed older page is asked for again, doubling with
  /// each failure in a row up to the last.
  static const _firstOlderRetry = Duration(seconds: 2);
  static const _lastOlderRetry = Duration(seconds: 30);

  final _localController = ScrollController();
  ScrollController get _controller => widget.controller ?? _localController;
  bool _atBottom = true;
  bool _stickToBottom = true;
  bool? _reportedAtBottom;
  bool _nearTopScheduled = false;

  /// The [ChatFlow.topKey] older history was last asked for.
  Object? _olderAskedFor = _notAsked;

  /// An older page was asked for and has not been answered yet: no row
  /// landed on top, and no build has shown the load over.
  bool _olderAwaited = false;

  /// The ask after a failed older page, and the pause the next failure gets.
  Timer? _olderRetry;
  Duration _olderRetryPause = _firstOlderRetry;

  @override
  void initState() {
    super.initState();
    _scheduleStick();
  }

  void _reportAtBottom() {
    // Scroll notifications arrive during layout. Notify the parent after the
    // frame, coalescing rapid changes so chips cannot cause a rebuild loop.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || _reportedAtBottom == _atBottom) return;
      _reportedAtBottom = _atBottom;
      widget.onAtBottomChanged?.call(_atBottom);
    });
  }

  @override
  void didUpdateWidget(ChatFlow oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.forceScrollToken != oldWidget.forceScrollToken) {
      _atBottom = true;
      _stickToBottom = true;
      _reportAtBottom();
    }
    if (_stickToBottom) _scheduleStick();
    if (widget.topKey != oldWidget.topKey) {
      // A page landed, or history was reset: nothing waits on the old top any
      // more, and the next failure starts again from the shortest pause.
      _olderAwaited = false;
      _olderRetry?.cancel();
      _olderRetry = null;
      _olderRetryPause = _firstOlderRetry;
    } else if (_olderAwaited &&
        !widget.loadingOlder &&
        widget.onNearTop != null) {
      // Over, and nothing landed in front: the page failed. A list too short
      // to scroll, or a reader staying at the top, never leaves the top to ask
      // again, so ask after a pause. Judged from the ask rather than from
      // loadingOlder turning false: a page that fails before a frame shows it
      // loading never turns it true for this widget to see.
      _olderAwaited = false;
      _retryOlderLater();
    }
    // Older history became available while the top was already in view. That
    // is news rather than a retry, so what was asked for before is forgotten.
    if (oldWidget.onNearTop == null &&
        widget.onNearTop != null &&
        _controller.hasClients) {
      _olderAskedFor = _notAsked;
      _checkNearTop(_controller.position);
    }
  }

  /// Ask for older history again after a pause, if the reader is still near
  /// the top by then or the list cannot scroll. Each failure in a row doubles
  /// the pause, up to [_lastOlderRetry].
  void _retryOlderLater() {
    _olderRetry?.cancel();
    final pause = _olderRetryPause;
    final doubled = pause * 2;
    _olderRetryPause = doubled > _lastOlderRetry ? _lastOlderRetry : doubled;
    _olderRetry = Timer(pause, () {
      _olderRetry = null;
      if (!mounted || !_controller.hasClients) return;
      _olderAskedFor = _notAsked;
      _checkNearTop(_controller.position);
    });
  }

  void _scheduleStick() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_controller.hasClients || !_stickToBottom) return;
      final max = _controller.position.maxScrollExtent;
      if ((_controller.offset - max).abs() > 1) {
        _controller.jumpTo(max);
      }
    });
  }

  void _checkNearTop(ScrollMetrics metrics) {
    if (widget.onNearTop == null) return;
    // A pinned list settles at its bottom whatever this frame shows, so a
    // long transcript opening at offset zero is not near its top.
    final resting = _stickToBottom ? metrics.maxScrollExtent : metrics.pixels;
    if (resting - metrics.minScrollExtent > _nearTop) {
      _olderAskedFor = _notAsked;
      return;
    }
    // Once per top row. A page that failed used to be asked for again on
    // every scroll update and streamed delta, back to back against a server
    // that was already failing.
    if (_nearTopScheduled || _olderAskedFor == widget.topKey) return;
    _olderAskedFor = widget.topKey;
    // Notifications can arrive mid-layout; call out once it is over.
    _nearTopScheduled = true;
    scheduleMicrotask(() {
      _nearTopScheduled = false;
      final ask = widget.onNearTop;
      if (!mounted || ask == null) return;
      // Answered by a row landing on top, or by a build that shows the load
      // over without one.
      _olderAwaited = true;
      ask();
    });
  }

  void _jumpToBottom() {
    _atBottom = true;
    _stickToBottom = true;
    _reportAtBottom();
    if (_controller.hasClients) {
      _controller.animateTo(
        _controller.position.maxScrollExtent,
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOut,
      );
    }
    setState(() {});
  }

  @override
  void dispose() {
    _olderRetry?.cancel();
    _localController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final older = widget.olderCount.clamp(0, widget.rows.length);
    return Stack(
      children: [
        NotificationListener<ScrollMetricsNotification>(
          onNotification: (notification) {
            if (notification.depth != 0) return false;
            if (_stickToBottom) _scheduleStick();
            _checkNearTop(notification.metrics);
            return false;
          },
          child: NotificationListener<ScrollNotification>(
            onNotification: (notification) {
              if (notification.depth != 0) return false;
              final metrics = notification.metrics;
              // A small intentional drag must release auto-stick immediately,
              // even before crossing the back-to-bottom button's threshold.
              if ((notification is ScrollStartNotification &&
                      notification.dragDetails != null) ||
                  (notification is UserScrollNotification &&
                      notification.direction != ScrollDirection.idle)) {
                _stickToBottom = false;
              } else if (notification is ScrollEndNotification) {
                _stickToBottom = metrics.extentAfter <= 1;
              }
              if (notification is ScrollUpdateNotification) {
                _checkNearTop(metrics);
              }
              final atBottom = metrics.extentAfter < 60;
              if (atBottom != _atBottom) {
                setState(() => _atBottom = atBottom);
                _reportAtBottom();
              }
              return false;
            },
            child: CustomScrollView(
              controller: _controller,
              keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
              center: _newestKey,
              slivers: [
                if (older > 0)
                  SliverPadding(
                    // Laid out upwards: the bottom inset meets the newer
                    // sliver's top one, making the usual 20 between rows.
                    padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
                    sliver: SliverList.separated(
                      itemCount: older,
                      // Index 0 is the row just above the opening one.
                      itemBuilder: (context, index) =>
                          widget.rows[older - 1 - index],
                      separatorBuilder: (_, _) => const SizedBox(height: 20),
                    ),
                  ),
                SliverPadding(
                  key: _newestKey,
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 20),
                  sliver: SliverList.separated(
                    itemCount: widget.rows.length - older,
                    itemBuilder: (context, index) => widget.rows[older + index],
                    separatorBuilder: (_, _) => const SizedBox(height: 20),
                  ),
                ),
              ],
            ),
          ),
        ),
        // Top fade mask.
        Positioned(
          top: 0,
          left: 0,
          right: 0,
          height: 10,
          child: IgnorePointer(
            child: DecoratedBox(
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topCenter,
                  end: Alignment.bottomCenter,
                  colors: [t.bg, t.bg.withValues(alpha: 0)],
                ),
              ),
            ),
          ),
        ),
        // Over the list rather than in it: a spinner inside the scroll
        // content would sit above the viewport exactly when someone has
        // scrolled to the top, and would change the extent it is loading for.
        if (widget.loadingOlder)
          Positioned(
            top: 8,
            left: 0,
            right: 0,
            child: IgnorePointer(
              child: Center(
                child: Material(
                  color: t.card,
                  shape: CircleBorder(side: BorderSide(color: t.hair)),
                  elevation: 1,
                  child: const Padding(
                    padding: EdgeInsets.all(6),
                    child: SizedBox.square(
                      dimension: 14,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    ),
                  ),
                ),
              ),
            ),
          ),
        // Bottom fade mask, only when scrolled up.
        Positioned(
          bottom: 0,
          left: 0,
          right: 0,
          height: 12,
          child: IgnorePointer(
            child: AnimatedOpacity(
              opacity: _atBottom ? 0 : 1,
              duration: const Duration(milliseconds: 150),
              child: DecoratedBox(
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    begin: Alignment.bottomCenter,
                    end: Alignment.topCenter,
                    colors: [t.bg, t.bg.withValues(alpha: 0)],
                  ),
                ),
              ),
            ),
          ),
        ),
        // Right rail, vertically centred — where the thumb and the eye
        // already are on a long scroll. Pinned to the bottom it sat directly
        // over the newest message and the composer's own controls.
        if (!_atBottom)
          Positioned(
            top: 0,
            bottom: 0,
            right: 14,
            child: Center(
              child: Material(
                color: t.card,
                shape: CircleBorder(side: BorderSide(color: t.hair)),
                elevation: 2,
                shadowColor: Colors.black26,
                child: InkWell(
                  customBorder: const CircleBorder(),
                  onTap: _jumpToBottom,
                  child: Padding(
                    padding: const EdgeInsets.all(9),
                    child: Icon(Icons.arrow_downward, size: 17, color: t.n700),
                  ),
                ),
              ),
            ),
          ),
      ],
    );
  }
}
