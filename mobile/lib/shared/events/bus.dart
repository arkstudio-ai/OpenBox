import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

/// In-app cross-feature event bus, mirroring frontend-v2
/// `shared/events/bus.ts`. Features never import each other; the sender
/// emits and whoever cares listens. The one live event today (as on web):
/// `workbench.open` `{kind: review|terminal|files, file?}`.
class AppEvent {
  const AppEvent(this.type, [this.payload = const {}]);

  final String type;
  final Map<String, Object?> payload;
}

class AppEventBus {
  final _controller = StreamController<AppEvent>.broadcast();

  Stream<AppEvent> get stream => _controller.stream;

  Stream<AppEvent> on(String type) =>
      _controller.stream.where((e) => e.type == type);

  void emit(String type, [Map<String, Object?> payload = const {}]) {
    _controller.add(AppEvent(type, payload));
  }
}

final appEventBusProvider = Provider<AppEventBus>((ref) => AppEventBus());
