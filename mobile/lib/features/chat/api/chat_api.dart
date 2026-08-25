import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/app_config.dart';
import '../../../shared/models/interaction.dart';
import '../../../shared/models/message.dart';
import '../../../shared/models/session.dart';

/// Chat REST calls (web `features/chat/api/*`). Everything the client sends
/// goes over REST; streaming arrives via WS.
class ChatApi {
  ChatApi(this._dio);

  final Dio _dio;

  Future<List<ChatMessage>> listMessages(String sessionId,
      {int offset = 0, int limit = 200}) async {
    final resp = await _dio.get<List<dynamic>>(
      '/api/agent/session/$sessionId/message',
      queryParameters: {'offset': offset, 'limit': limit},
    );
    return (resp.data ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(ChatMessage.fromJson)
        .toList();
  }

  Future<Session> getSession(String sessionId) async {
    final resp = await _dio
        .get<Map<String, dynamic>>('/api/agent/session/$sessionId');
    return Session.fromJson(resp.data ?? const {});
  }

  /// First-send session creation (web `useStartChat` → `useCreateSession`).
  Future<Session> createSession({
    String? projectId,
    String model = '',
    String agent = 'build',
  }) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/api/agent/session',
      data: {
        'project_id': ?projectId,
        'model': model,
        'agent': agent,
      },
    );
    return Session.fromJson(resp.data ?? const {});
  }

  /// `POST …/prompt_async` → `{ok: true}`; output arrives over WS.
  Future<void> promptAsync(
    String sessionId, {
    required String text,
    required String clientMessageId,
    String? agent,
    String? model,
    List<String> attachments = const [],
  }) async {
    await _dio.post<dynamic>(
      '/api/agent/session/$sessionId/prompt_async',
      data: {
        'text': text,
        'client_message_id': clientMessageId,
        'agent': ?agent,
        if (model != null && model.isNotEmpty) 'model': model,
        if (attachments.isNotEmpty) 'attachments': attachments,
      },
    );
  }

  Future<void> abort(String sessionId) async {
    await _dio.post<dynamic>('/api/agent/session/$sessionId/abort');
  }

  Future<void> regenerate(String sessionId, String messageId,
      {String? model}) async {
    await _dio.post<dynamic>(
      '/api/agent/session/$sessionId/regenerate/$messageId',
      data: {'model': ?model},
    );
  }

  Future<void> dismissMessage(String sessionId, String messageId) async {
    await _dio
        .delete<dynamic>('/api/agent/session/$sessionId/message/$messageId');
  }

  Future<void> setReaction(
      String sessionId, String messageId, String? reaction) async {
    await _dio.post<dynamic>(
      '/api/agent/session/$sessionId/message/$messageId/reaction',
      data: {'reaction': reaction},
    );
  }

  Future<void> acceptPlan(String sessionId) async {
    await _dio.post<dynamic>('/api/agent/session/$sessionId/plan/accept');
  }

  Future<void> rejectPlan(String sessionId) async {
    await _dio.post<dynamic>('/api/agent/session/$sessionId/plan/reject');
  }

  Future<AppConfig> getConfig() async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/agent/config');
    return AppConfig.fromJson(resp.data ?? const {});
  }

  Future<List<AgentInfo>> listAgents() async {
    final resp = await _dio.get<List<dynamic>>('/api/agent/agent');
    return (resp.data ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(AgentInfo.fromJson)
        .toList();
  }

  Future<List<PermissionRequest>> listPermissions() async {
    final resp = await _dio.get<List<dynamic>>('/api/agent/permission');
    return (resp.data ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(PermissionRequest.fromJson)
        .toList();
  }

  /// Backend-native actions: `once` | `always` | `reject`.
  Future<void> replyPermission(String requestId, String action) async {
    await _dio.post<dynamic>('/api/agent/permission/$requestId',
        data: {'action': action});
  }

  Future<List<QuestionRequest>> listQuestions() async {
    final resp = await _dio.get<List<dynamic>>('/api/agent/question');
    return (resp.data ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(QuestionRequest.fromJson)
        .toList();
  }

  /// One label-array per question, in order (nested array shape).
  Future<void> replyQuestion(String requestId, List<List<String>> answers) async {
    await _dio.post<dynamic>('/api/agent/question/$requestId',
        data: {'answers': answers});
  }

  Future<void> rejectQuestion(String requestId) async {
    await _dio.post<dynamic>('/api/agent/question/$requestId/reject');
  }
}

final chatApiProvider =
    Provider<ChatApi>((ref) => ChatApi(ref.watch(apiDioProvider)));
