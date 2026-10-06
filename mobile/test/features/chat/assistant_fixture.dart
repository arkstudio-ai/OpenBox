import 'dart:async';
import 'dart:convert';

import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/state/assistant_controller.dart';
import 'package:bossip_mobile/features/chat/state/config_providers.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/api/containers_api.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/app_config.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

const scope = (userId: 'owner', workspaceId: 'workspace');

ChatMessage message(
  String id, {
  String text = 'verified answer',
  String role = 'assistant',
  String? clientId,
}) => ChatMessage.fromJson({
  'id': id,
  'session_id': 'main',
  'role': role,
  'finish': 'stop',
  'client_message_id': clientId,
  'parts': [
    {'id': '$id-text', 'type': 'text', 'text': text},
  ],
});

class TestWs extends AgentWsClient {
  TestWs() : super(Dio());
  final frames = StreamController<WsEvent>.broadcast(sync: true);
  @override
  Stream<WsEvent> get events => frames.stream;
  @override
  Future<void> connect() async {}
  Future<void> close() => frames.close();
}

class TestApi extends AssistantApi {
  TestApi() : super(Dio(), scope);
  bool created = true;
  int ensures = 0;
  int cursor = 1;
  bool gap = false;
  bool failCommand = false;

  /// History reads fail while set; [historyGate] holds them.
  bool failHistory = false;
  Completer<void>? historyGate;
  int revision = 1;
  String taskObserved = 'running', taskDesired = 'running';
  String sessionStatus = 'idle';
  Future<Map<String, dynamic>> Function(Map<String, dynamic>)? onSend;

  /// Each history read's `before` (null for the newest turns).
  final historyReads = <String?>[];
  final eventCursors = <String>[];
  final sends = <Map<String, dynamic>>[];
  final controls = <Map<String, dynamic>>[];
  final reads = <Map<String, dynamic>>[];
  final replies = <Map<String, dynamic>>[];
  bool failReply = false;
  Completer<void>? replyGate;
  final archives = <Map<String, dynamic>>[];
  final reactions = <Map<String, Object?>>[];
  final forgotten = <Map<String, Object?>>[];

  /// What "我的任务" lists; empty unless a test fills it.
  List<Map<String, dynamic>> watchItems = [];
  bool watchHasMore = false;
  List<Map<String, dynamic>> waiting = [];
  final stored = <String, ChatMessage>{'m02': message('m02')};
  List<String> newest = ['m02'];
  List<String> older = [];
  @override
  Future<AssistantSnapshot> snapshot({String? taskCursor}) async =>
      AssistantSnapshot({
        'state': created ? 'ready' : 'not_created',
        if (created)
          'session': {
            'id': 'main',
            'user_id': scope.userId,
            'workspace_id': scope.workspaceId,
            'project_id': 'default',
            'kind': 'assistant',
            'agent': 'assistant',
            'status': sessionStatus,
            'model': 'test/model',
          },
        'event_cursor': 'cursor-$cursor',
        'high_water_mark': cursor,
        'last_seen_sequence': 0,
        'answers': [
          {
            'message_id': 'm02',
            'sequence': 2,
            'available': true,
            'display_token': 'signed-display',
          },
        ],
        'tasks': [
          {
            'task': {
              'id': 'task',
              'title': 'Original task',
              'execution_session_id': 'execution',
              'desired_state': taskDesired,
              'observed_state': taskObserved,
              'control_revision': revision,
              'intent_revision': 1,
              'updated_at': DateTime.now()
                  .subtract(const Duration(hours: 2))
                  .toUtc()
                  .toIso8601String(),
            },
            'execution_session': {'id': 'execution', 'status': 'idle'},
            'run_binding': {'run_id': 'run-1', 'generation': 1},
            'latest_result': {
              'result_id': 'result',
              'outcome': 'error',
              'delivery_state': 'processed',
              'report_attempt': 1,
              'assistant_inbox_id': 'report-inbox',
              'observed_intent_revision': 1,
            },
          },
        ],
      });
  @override
  Future<void> ensure() async {
    ensures++;
    created = true;
  }

  @override
  Future<AssistantTask> task(String id) async => (await snapshot()).tasks.first;

  @override
  Future<Map<String, dynamic>> requests(String kind, {String? cursor}) async =>
      {'items': <Map<String, dynamic>>[], 'receipts': <Map<String, dynamic>>[]};

  @override
  Future<Map<String, dynamic>> waitingQuestions() async => {'items': waiting};

  @override
  Future<Map<String, dynamic>> watch() async => {
    'items': watchItems,
    'has_more': watchHasMore,
  };

  @override
  Future<Map<String, dynamic>> archive(
    String taskId,
    Map<String, dynamic> body,
  ) async {
    archives.add({'task_id': taskId, ...body});
    return {
      'command_id': 'archive-command',
      'task_id': taskId,
      'execution_session_id': 'execution',
      'task_revision': (body['expected_revision'] as int) + 1,
      'state': 'archived',
    };
  }

  @override
  Future<void> setReaction(
    String sessionId,
    String messageId,
    String? reaction,
  ) async {
    reactions.add({
      'session': sessionId,
      'message': messageId,
      'reaction': reaction,
    });
  }

  @override
  Future<void> forgetMemory(
    String memoryId, {
    int? revision,
    required String requestId,
  }) async {
    forgotten.add({
      'memory': memoryId,
      'revision': revision,
      'request': requestId,
    });
  }

  @override
  Future<Map<String, dynamic>> events(String cursor) async {
    eventCursors.add(cursor);
    return {'state': gap ? 'snapshot_required' : 'ready'};
  }

  @override
  Future<HistoryPage> history(String sessionId, {String? before}) async {
    historyReads.add(before);
    await historyGate?.future;
    if (failHistory) throw StateError('History read failed');
    return HistoryPage(
      messages: [for (final id in before == null ? newest : older) stored[id]!],
      hasMore: before == null && older.isNotEmpty,
    );
  }

  Map<String, dynamic> receipt(Map<String, dynamic> body) => {
    'assistant_session_id': 'main',
    'client_id': body['client_id'],
    'inbox_id': 'inbox',
    'delivery': 'followup',
    'state': 'accepted',
  };
  @override
  Future<Map<String, dynamic>> send(Map<String, dynamic> body) async {
    sends.add(Map<String, dynamic>.from(jsonDecode(jsonEncode(body)) as Map));
    return onSend == null ? receipt(body) : onSend!(body);
  }

  @override
  Future<Map<String, dynamic>> control(
    String taskId,
    Map<String, dynamic> body,
  ) async {
    controls.add({...body});
    if (failCommand) {
      throw ApiError(status: 503, code: 'NETWORK', message: 'Lost reply');
    }
    return {'command_id': 'control', 'state': 'accepted'};
  }

  @override
  Future<void> markRead(Map<String, dynamic> answer) async {
    reads.add(answer);
  }

  @override
  Future<Map<String, dynamic>> reply(
    String kind,
    String id,
    Map<String, dynamic> body, {
    bool reject = false,
  }) async {
    replies.add({'kind': kind, 'id': id, 'reject': reject, ...body});
    await replyGate?.future;
    if (failReply) {
      throw ApiError(status: 503, code: 'NETWORK', message: 'Lost reply');
    }
    return {
      'command_id': 'reply-command',
      'state': 'accepted',
      'request_id': id,
      'request_kind': kind,
      'reply_id': body['reply_id'],
      'assistant_session_id': 'main',
      'request_revision': body['expected_request_revision'],
      'options_hash': body['options_hash'],
    };
  }
}

class Fixture {
  Fixture(this.prefs, {TestApi? server, I18nState? i18n, ChatApi? legacy})
    : api = server ?? TestApi() {
    container = ProviderContainer(
      overrides: [
        prefsProvider.overrideWithValue(prefs),
        wsClientProvider.overrideWithValue(ws),
        assistantScopeProvider.overrideWithValue(scope),
        assistantApiProvider(scope).overrideWithValue(api),
        if (legacy != null) chatApiProvider.overrideWithValue(legacy),
        appConfigProvider.overrideWith(
          (ref) async => AppConfig.fromJson({
            'models': <Map<String, dynamic>>[],
            'default_model': 'test/model',
          }),
        ),
        runningContainerProvider.overrideWith(
          (ref) => throw StateError('Assistant discovered a desktop'),
        ),
        if (i18n != null)
          i18nProvider.overrideWith(() => FixedI18n(i18n, prefs)),
      ],
    );
    container.read(authSessionProvider).userId = scope.userId;
    container.read(workspaceScopeProvider).currentId = scope.workspaceId;
    subscription = container.listen(
      assistantControllerProvider(scope),
      (_, _) {},
    );
  }
  final SharedPreferences prefs;
  final TestApi api;
  final ws = TestWs();
  late final ProviderContainer container;
  late final ProviderSubscription<AssistantState> subscription;
  bool closed = false;
  AssistantController get controller =>
      container.read(assistantControllerProvider(scope).notifier);
  AssistantState get state =>
      container.read(assistantControllerProvider(scope));
  Future<void> ready() async {
    await controller.refresh();
    await Future<void>.delayed(Duration.zero);
  }

  void close() {
    if (closed) return;
    closed = true;
    container.dispose();
    unawaited(ws.close());
  }
}

class FixedI18n extends I18nController {
  FixedI18n(this.value, SharedPreferences prefs) : super(value.bundle, prefs);
  final I18nState value;
  @override
  I18nState build() => value;
}
