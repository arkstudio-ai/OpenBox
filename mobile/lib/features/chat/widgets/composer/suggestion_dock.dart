import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';

/// Keep the dock pinned while forwarding vertical gestures to chat history.
class SuggestionDock extends StatefulWidget {
  const SuggestionDock({super.key, required this.child, this.controller});

  final Widget child;
  final ScrollController? controller;

  @override
  State<SuggestionDock> createState() => _SuggestionDockState();
}

class _SuggestionDockState extends State<SuggestionDock> {
  Drag? _drag;

  @override
  void dispose() {
    _drag?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final controller = widget.controller;
    if (controller == null) return widget.child;
    return Listener(
      onPointerSignal: (event) {
        if (event is! PointerScrollEvent || !controller.hasClients) return;
        GestureBinding.instance.pointerSignalResolver.register(event, (_) {
          if (controller.hasClients) {
            controller.position.pointerScroll(event.scrollDelta.dy);
          }
        });
      },
      child: GestureDetector(
        behavior: HitTestBehavior.translucent,
        onVerticalDragStart: (details) {
          _drag?.cancel();
          if (controller.hasClients) {
            _drag = controller.position.drag(details, () => _drag = null);
          }
        },
        onVerticalDragUpdate: (details) => _drag?.update(details),
        onVerticalDragEnd: (details) => _drag?.end(details),
        onVerticalDragCancel: () => _drag?.cancel(),
        child: widget.child,
      ),
    );
  }
}
