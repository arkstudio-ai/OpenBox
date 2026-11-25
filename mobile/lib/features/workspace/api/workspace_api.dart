import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/project.dart';
import '../../../shared/models/session.dart';
import '../../../shared/models/workspace.dart';

/// Sessions + projects REST calls (web `features/workspace/api/*`).
class WorkspaceApi {
  WorkspaceApi(this._dio);

  final Dio _dio;

  Future<WorkspaceListResult> listWorkspaces() async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/workspaces');
    return WorkspaceListResult.fromJson(resp.data ?? const {});
  }

  Future<WorkspaceDetail> getCurrentWorkspace() async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/workspaces/current',
    );
    return WorkspaceDetail.fromJson(resp.data ?? const {});
  }

  Future<List<WorkspaceInvitation>> listPendingInvitations() async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/workspaces/invitations/pending',
    );
    return (resp.data?['items'] as List<dynamic>? ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(WorkspaceInvitation.fromJson)
        .toList();
  }

  Future<WorkspaceInvitation> inviteMember({
    required String workspaceId,
    required String target,
    required WorkspaceRole role,
  }) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/api/workspaces/$workspaceId/invitations',
      data: {'target': target, 'role': role.wire},
    );
    return WorkspaceInvitation.fromJson(resp.data ?? const {});
  }

  Future<void> changeMemberRole({
    required String workspaceId,
    required String userId,
    required WorkspaceRole role,
  }) async {
    await _dio.patch<dynamic>(
      '/api/workspaces/$workspaceId/members/$userId',
      data: {'role': role.wire},
    );
  }

  Future<void> removeMember({
    required String workspaceId,
    required String userId,
  }) async {
    await _dio.delete<dynamic>('/api/workspaces/$workspaceId/members/$userId');
  }

  Future<String> acceptInvitation(String token) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/api/workspaces/invitations/${Uri.encodeComponent(token)}/accept',
    );
    return resp.data?['workspace_id'] as String? ?? '';
  }

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
      data: {'project_id': ?projectId, 'model': model, 'agent': agent},
    );
    return Session.fromJson(resp.data ?? const {});
  }

  Future<Session> getSession(String id) async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/agent/session/$id');
    return Session.fromJson(resp.data ?? const {});
  }

  Future<void> renameSession(String id, String title) async {
    await _dio.patch<dynamic>('/api/agent/session/$id', data: {'title': title});
  }

  Future<({List<String> deleted, List<String> failed})> deleteSessions(
    List<String> ids,
  ) async {
    try {
      final response = await _dio.post<Map<String, dynamic>>(
        '/api/agent/session/batch-delete',
        data: {'session_ids': ids},
      );
      return (
        deleted: asList(response.data?['deleted']).whereType<String>().toList(),
        failed: asList(response.data?['failed']).whereType<String>().toList(),
      );
    } on DioException catch (error) {
      final status = error.response?.statusCode;
      if (status != 404 && status != 405) rethrow;
    }
    // The App can be released before the server adds the batch endpoint.
    final deleted = <String>[];
    final failed = <String>[];
    for (final id in ids) {
      try {
        await deleteSession(id);
        deleted.add(id);
      } on DioException {
        failed.add(id);
      }
    }
    return (deleted: deleted, failed: failed);
  }

  Future<void> deleteSession(String id) async {
    await _dio.delete<dynamic>('/api/agent/session/$id');
  }

  Future<Project> createProject(String name) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/api/agent/project',
      data: {'name': name},
    );
    return Project.fromJson(resp.data ?? const {});
  }

  Future<void> renameProject(String id, String name) async {
    await _dio.patch<dynamic>('/api/agent/project/$id', data: {'name': name});
  }

  Future<void> deleteProject(String id) async {
    await _dio.delete<dynamic>('/api/agent/project/$id');
  }
}

final workspaceApiProvider = Provider<WorkspaceApi>(
  (ref) => WorkspaceApi(ref.watch(apiDioProvider)),
);
