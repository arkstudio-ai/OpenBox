import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/project.dart';
import '../../../shared/models/session.dart';

/// Sessions + projects REST calls (web `features/workspace/api/*`).
class WorkspaceApi {
  WorkspaceApi(this._dio);

  final Dio _dio;

  Future<List<Session>> listSessions() async {
    final resp = await _dio.get<List<dynamic>>('/api/agent/session');
    return (resp.data ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(Session.fromJson)
        .toList();
  }

  Future<List<Project>> listProjects() async {
    final resp = await _dio.get<List<dynamic>>('/api/agent/project');
    return (resp.data ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(Project.fromJson)
        .toList();
  }

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

  Future<Session> getSession(String id) async {
    final resp =
        await _dio.get<Map<String, dynamic>>('/api/agent/session/$id');
    return Session.fromJson(resp.data ?? const {});
  }

  Future<void> renameSession(String id, String title) async {
    await _dio.patch<dynamic>('/api/agent/session/$id', data: {'title': title});
  }

  Future<void> deleteSession(String id) async {
    await _dio.delete<dynamic>('/api/agent/session/$id');
  }

  Future<Project> createProject(String name) async {
    final resp = await _dio
        .post<Map<String, dynamic>>('/api/agent/project', data: {'name': name});
    return Project.fromJson(resp.data ?? const {});
  }

  Future<void> renameProject(String id, String name) async {
    await _dio.patch<dynamic>('/api/agent/project/$id', data: {'name': name});
  }

  Future<void> deleteProject(String id) async {
    await _dio.delete<dynamic>('/api/agent/project/$id');
  }
}

final workspaceApiProvider =
    Provider<WorkspaceApi>((ref) => WorkspaceApi(ref.watch(apiDioProvider)));
