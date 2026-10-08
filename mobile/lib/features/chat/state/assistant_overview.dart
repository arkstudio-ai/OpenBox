import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/events/app_lifecycle.dart';
import '../../../shared/ws/ws_client.dart';
import '../api/assistant_api.dart';

/// Read-only drawer badge: opening the shell never ensures a main session or
/// loads its transcript. Counts come from the same user-level SQL read cursor.
final assistantOverviewProvider = FutureProvider.autoDispose
    .family<AssistantSnapshot, AssistantScope>((ref, scope) {
      void refresh() {
        if (ref.read(appVisibleProvider)) ref.invalidateSelf();
      }

      final timer = Timer.periodic(
        const Duration(seconds: 30),
        (_) => refresh(),
      );
      final events = ref.read(wsClientProvider).events.listen((event) {
        if (event.type == '__connected' ||
            event.type.startsWith('assistant.')) {
          refresh();
        }
      });
      ref.listen(appVisibleProvider, (previous, visible) {
        if (previous == false && visible) refresh();
      });
      ref.onDispose(() {
        timer.cancel();
        unawaited(events.cancel());
      });
      return ref.read(assistantApiProvider(scope)).snapshot();
    });
