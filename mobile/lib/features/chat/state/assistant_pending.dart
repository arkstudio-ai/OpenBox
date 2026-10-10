import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/events/app_lifecycle.dart';
import '../../../shared/events/bus.dart';
import '../../../shared/models/json.dart';
import '../../../shared/ws/ws_client.dart';
import '../api/assistant_api.dart';

/// Includes unanswered questions outside the assistant's followed tasks.
class AssistantPending {
  AssistantPending(List<Map<String, dynamic>> pages) {
    final requests = <String, Map<String, dynamic>>{};
    for (var i = 0; i < pages.length; i++) {
      for (final row in asList(
        pages[i]['items'],
      ).whereType<Map<String, dynamic>>()) {
        requests['${i == 1 ? 'p' : 'q'}:${row['id']}'] = row;
      }
      for (final row in asList(
        pages[i]['receipts'],
      ).whereType<Map<String, dynamic>>()) {
        if (row['state'] == 'failed') ids.add('failed:${row['command_id']}');
      }
      hasMore = hasMore || asString(pages[i]['next_cursor']) != null;
    }
    count = requests.length;
    ids.addAll(requests.keys);
    sessions.addAll(
      requests.values.map((row) => asString(row['session_id']) ?? ''),
    );
  }
  late final int count;
  final ids = <String>[];
  final sessions = <String>{};
  bool hasMore = false;
}

final assistantPendingProvider = FutureProvider.autoDispose
    .family<AssistantPending, AssistantScope>((ref, scope) async {
      Timer? hint;
      void refresh() {
        if (ref.read(appVisibleProvider)) ref.invalidateSelf();
      }

      void hinted() {
        hint ??= Timer(const Duration(milliseconds: 500), () {
          hint = null;
          refresh();
        });
      }

      final timer = Timer.periodic(
        const Duration(seconds: 30),
        (_) => refresh(),
      );
      final ws = ref.read(wsClientProvider).events.listen((event) {
        if (event.type == '__connected' ||
            event.type.startsWith('assistant.') ||
            event.type.startsWith('question.') ||
            event.type.startsWith('permission.')) {
          hinted();
        }
      });
      final bus = ref.read(appEventBusProvider).stream.listen((event) {
        if (event.type == 'question.resolved' ||
            event.type == 'assistant.request.changed') {
          hinted();
        }
      });
      ref.listen(appVisibleProvider, (previous, visible) {
        if (previous == false && visible) refresh();
      });
      ref.onDispose(() {
        timer.cancel();
        hint?.cancel();
        unawaited(ws.cancel());
        unawaited(bus.cancel());
      });
      final api = ref.read(assistantApiProvider(scope));
      return AssistantPending(
        await Future.wait([
          api.requests('question'),
          api.requests('permission'),
          api.waitingQuestions(),
        ]),
      );
    });

String reminderStorageKey(AssistantScope scope) =>
    'assistant-reminder:${scope.userId}:${scope.workspaceId}';

final assistantDismissedReminderProvider = StateProvider.autoDispose
    .family<List<String>, AssistantScope>(
      (ref, scope) =>
          ref.read(prefsProvider).getStringList(reminderStorageKey(scope)) ??
          [],
    );
