import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/ws/ws_client.dart';

final sessionStreamSubscriptionProvider = Provider.autoDispose
    .family<void, String>((ref, sessionId) {
      final release = ref.watch(wsClientProvider).subscribeSessions([
        sessionId,
      ]);
      ref.onDispose(release);
    });
