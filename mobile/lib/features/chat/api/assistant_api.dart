import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/api/workspace_scope.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/message.dart';
import '../../../shared/models/session.dart';
import 'chat_api.dart';

typedef AssistantScope = ({String userId, String workspaceId});

/// The app composition layer binds this to its authenticated workspace store.
/// Chat itself has no dependency on another feature's state.
final assistantScopeProvider = Provider<AssistantScope?>((ref) => null);

class AssistantTask {
  AssistantTask(this.data);
  final Map<String, dynamic> data;
  Map<String, dynamic> get task => asMap(data['task']);
  String get id => asString(task['id']) ?? '';
  String get title => asString(task['title']) ?? '';
  String get executionId => asString(task['execution_session_id']) ?? '';
  String get observed => asString(task['observed_state']) ?? 'unknown';
  String get desired => asString(task['desired_state']) ?? 'running';
  int get revision => asInt(task['control_revision']) ?? 0;
  Map<String, dynamic> get result => asMap(data['latest_result']);
  Map<String, dynamic> get submission => asMap(data['latest_submission']);
  Map<String, dynamic>? get run {
    final binding = asMap(data['run_binding']);
    return binding['run_id'] is String && binding['phase'] != 'idle'
        ? {'run_id': binding['run_id'], 'generation': binding['generation']}
        : null;
  }
}

class AssistantSnapshot {
  AssistantSnapshot(this.data);
  final Map<String, dynamic> data;
  Session? get session => data['session'] is Map<String, dynamic>
      ? Session.fromJson(asMap(data['session']))
      : null;
  String? get cursor => asString(data['event_cursor']);
  int get highWater => asInt(data['high_water_mark']) ?? 0;
  int get lastSeen => asInt(data['last_seen_sequence']) ?? 0;
  int get unreadCount => asInt(data['unread_count']) ?? 0;
  String? get taskCursor => asString(data['next_task_cursor']);
  List<AssistantTask> get tasks => [
    for (final task in asList(data['tasks']))
      if (task is Map<String, dynamic>) AssistantTask(task),
  ];
  List<Map<String, dynamic>> get answers =>
      asList(data['answers']).whereType<Map<String, dynamic>>().toList();
}

/// Every request freezes both actor and workspace, including token-refresh
/// replay. This API never discovers, creates or restores a desktop.
class AssistantApi {
  AssistantApi(this.dio, this.scope);
  final Dio dio;
  final AssistantScope scope;

  Options get options => Options(
    headers: {'X-Workspace-Id': scope.workspaceId},
    extra: {
      requestScopeUserKey: scope.userId,
      requestScopeWorkspaceKey: scope.workspaceId,
    },
    listFormat: ListFormat.multi,
  );

  Future<Map<String, dynamic>> _get(
    String path, [
    Map<String, dynamic>? query,
  ]) async =>
      (await dio.get<Map<String, dynamic>>(
        path,
        queryParameters: query,
        options: options,
      )).data ??
      {};

  Future<Map<String, dynamic>> _post(
    String path,
    Map<String, dynamic> body,
  ) async =>
      (await dio.post<Map<String, dynamic>>(
        path,
        data: body,
        options: options,
      )).data ??
      {};

  Future<AssistantSnapshot> snapshot({String? taskCursor}) async =>
      AssistantSnapshot(
        await _get('/api/assistant', {'task_cursor': ?taskCursor}),
      );

  Future<Map<String, dynamic>> resultTarget(String id) =>
      _get('/api/assistant/results/${Uri.encodeComponent(id)}/target');

  Future<void> ensure() async {
    await _post('/api/assistant/ensure', {});
  }

  Future<Session> entrySession(String id) async => Session.fromJson(
    await _get('/api/agent/session/${Uri.encodeComponent(id)}'),
  );

  Future<Map<String, dynamic>> requests(String kind, {String? cursor}) =>
      _get('/api/assistant/requests', {'kind': kind, 'cursor': ?cursor});

  Future<Map<String, dynamic>> reviewRequest(String kind, String id) =>
      _get('/api/assistant/requests/$kind/${Uri.encodeComponent(id)}/review');

  Future<Map<String, dynamic>> requestDisplayed(String token) =>
      _post('/api/assistant/requests/displayed', {'display_token': token});

  Future<Map<String, dynamic>> reply(
    String kind,
    String id,
    Map<String, dynamic> body, {
    bool reject = false,
  }) => _post(
    '/api/agent/$kind/${Uri.encodeComponent(id)}${reject ? '/reject' : ''}',
    body,
  );

  Future<Map<String, dynamic>> events(String cursor) =>
      _get('/api/assistant/events', {'after': cursor});

  Future<HistoryPage> history(String sessionId, {String? before}) async {
    final data = await _get('/api/agent/session/$sessionId/history', {
      'turns': 8,
      'before': ?before,
    });
    return HistoryPage(
      messages: _messages(data),
      hasMore: data['has_more'] == true,
    );
  }

  static List<ChatMessage> _messages(Map<String, dynamic> data) => [
    for (final message in asList(data['messages']))
      if (message is Map<String, dynamic>) ChatMessage.fromJson(message),
  ];

  Future<List<ChatMessage>> messages(
    String sessionId,
    List<String> ids,
  ) async => _messages(
    await _get('/api/assistant/messages', {
      'session_id': sessionId,
      'message_ids': ids,
    }),
  );

  Future<Map<String, dynamic>> send(Map<String, dynamic> body) =>
      _post('/api/assistant/turns', body);

  Future<AssistantTask> task(String id) async => AssistantTask(
    await _get('/api/assistant/tasks/${Uri.encodeComponent(id)}'),
  );

  Future<Map<String, dynamic>> sessions({String? cursor}) =>
      _get('/api/assistant/sessions', {'limit': 20, 'cursor': ?cursor});

  Future<Map<String, dynamic>> linkExisting(Map<String, dynamic> body) =>
      _post('/api/assistant/tasks/link', body);

  Future<Map<String, dynamic>> control(
    String taskId,
    Map<String, dynamic> body,
  ) => _post(
    '/api/assistant/tasks/${Uri.encodeComponent(taskId)}/commands',
    body,
  );

  Future<Map<String, dynamic>> retryReport(
    String resultId,
    Map<String, dynamic> body,
  ) => _post(
    '/api/assistant/results/${Uri.encodeComponent(resultId)}/retry',
    body,
  );

  Future<Map<String, dynamic>> result(
    String id, {
    int offset = 0,
    String? version,
  }) => _get('/api/assistant/results/${Uri.encodeComponent(id)}', {
    'max_chars': 8000,
    'offset': offset,
    'source_version': ?version,
  });

  Future<void> markRead(Map<String, dynamic> answer) async {
    await _post('/api/assistant/read-cursor', {
      'last_seen_sequence': answer['sequence'],
      'display_token': answer['display_token'],
    });
  }
}

final assistantApiProvider = Provider.family<AssistantApi, AssistantScope>(
  (ref, scope) => AssistantApi(ref.watch(apiDioProvider), scope),
);
