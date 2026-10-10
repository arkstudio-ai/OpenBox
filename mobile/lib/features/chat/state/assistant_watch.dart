import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/events/app_lifecycle.dart';
import '../../../shared/models/json.dart';
import '../../../shared/ws/ws_client.dart';
import '../api/assistant_api.dart';
import '../utils/task_status.dart';

/// One conversation the personal assistant follows (web
/// `AssistantWatchItem`): what "我的任务" lists and what a task card borrows
/// for its project name and latest word.
class AssistantWatchItem {
  AssistantWatchItem(this.data);

  final Map<String, dynamic> data;

  String get taskId => asString(data['task_id']) ?? '';
  String get title => asString(data['title']) ?? '';
  String get projectName => asString(asMap(data['project'])['name']) ?? '';
  String get sessionId => asString(data['session_id']) ?? '';
  String? get sessionStatus => asString(data['session_status']);
  String? get desiredState => asString(data['desired_state']);
  String? get observedState => asString(data['observed_state']);
  int get pendingQuestions => asInt(data['pending_questions']) ?? 0;
  Map<String, dynamic> get latestResult => asMap(data['latest_result']);

  /// The newest result's summary as plain words, when it said anything.
  String? get summary {
    final text = plainSummary(asString(latestResult['summary']) ?? '');
    return text.isEmpty ? null : text;
  }

  TaskStatus get status => taskStatus(
    sessionStatus: sessionStatus,
    observedState: observedState,
    desiredState: desiredState,
    pendingQuestions: pendingQuestions,
    outcome: asString(latestResult['outcome']),
  );
}

class AssistantWatchPage {
  AssistantWatchPage(Map<String, dynamic> data)
    : items = [
        for (final item in asList(data['items']))
          if (item is Map<String, dynamic>) AssistantWatchItem(item),
      ],
      hasMore = data['has_more'] == true;

  final List<AssistantWatchItem> items;
  final bool hasMore;

  AssistantWatchItem? byTask(String taskId) =>
      items.where((item) => item.taskId == taskId).firstOrNull;
}

/// The followed conversations: one cheap read. Socket hints refresh it
/// (coalesced, since a run settling emits several) and a slow timer covers
/// missed ones (web `useAssistantWatch`).
final assistantWatchProvider = FutureProvider.autoDispose
    .family<AssistantWatchPage, AssistantScope>((ref, scope) {
      AssistantWatchPage? page;
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
      final events = ref.read(wsClientProvider).events.listen((event) {
        if (event.type == 'session.status') {
          // A followed conversation changed state, or any run settled — the
          // assistant's own run may have started or stopped following one.
          final status = event.data['status'];
          final followed =
              page?.items.any((item) => item.sessionId == event.sessionId) ??
              false;
          if (followed || status == 'idle' || status == 'error') hinted();
        } else if (event.type == '__connected' ||
            event.type.startsWith('assistant.') ||
            event.type.startsWith('question.')) {
          // Pending questions are counted per followed conversation.
          hinted();
        }
      });
      ref.listen(appVisibleProvider, (previous, visible) {
        if (previous == false && visible) refresh();
      });
      ref.onDispose(() {
        timer.cancel();
        hint?.cancel();
        unawaited(events.cancel());
      });
      return ref
          .read(assistantApiProvider(scope))
          .watch()
          .then((data) => page = AssistantWatchPage(data));
    });
