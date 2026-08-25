import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';

/// Scrolling chat column (web `ChatFlow.tsx`): stick-to-bottom while
/// streaming (re-pin on content growth), force scroll on send, top/bottom
/// fade masks, back-to-bottom FAB.
class ChatFlow extends StatefulWidget {
  const ChatFlow({
    super.key,
    required this.rows,
    this.forceScrollToken,
  });

  final List<Widget> rows;

  /// Changes when the user sends → force pin + jump (web :128-141).
  final Object? forceScrollToken;

  @override
  State<ChatFlow> createState() => _ChatFlowState();
}

class _ChatFlowState extends State<ChatFlow> {
  final _controller = ScrollController();
  bool _atBottom = true;

  @override
  void didUpdateWidget(ChatFlow oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.forceScrollToken != oldWidget.forceScrollToken) {
      _atBottom = true;
    }
    if (_atBottom) _scheduleStick();
  }

  void _scheduleStick() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_controller.hasClients || !_atBottom) return;
      final max = _controller.position.maxScrollExtent;
      if ((_controller.offset - max).abs() > 1) {
        _controller.jumpTo(max);
      }
    });
  }

  void _jumpToBottom() {
    _atBottom = true;
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
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Stack(
      children: [
        NotificationListener<ScrollNotification>(
          onNotification: (notification) {
            final metrics = notification.metrics;
            final atBottom = metrics.extentAfter < 60;
            if (atBottom != _atBottom) {
              setState(() => _atBottom = atBottom);
            }
            return false;
          },
          child: ListView.separated(
            controller: _controller,
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 20),
            keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
            itemCount: widget.rows.length,
            separatorBuilder: (_, _) => const SizedBox(height: 20),
            itemBuilder: (context, index) => widget.rows[index],
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
        if (!_atBottom)
          Positioned(
            right: 14,
            bottom: 14,
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
      ],
    );
  }
}
