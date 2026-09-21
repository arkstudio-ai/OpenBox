import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/api/auth_session.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/api/workspace_scope.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/team.dart';

final teamsApiProvider = Provider<TeamsApi>(
  (ref) => TeamsApi(
    ref.watch(apiDioProvider),
    ref.watch(authSessionProvider),
    ref.watch(workspaceScopeProvider),
  ),
);

class TeamsApi {
  TeamsApi(this._dio, this._auth, this._workspace);
  final Dio _dio;
  final AuthSession _auth;
  final WorkspaceScope _workspace;

  void _check(TeamScope scope) {
    if (_auth.userId != scope.userId ||
        _workspace.currentId != scope.workspaceId) {
      throw ApiError(
        status: 403,
        code: 'WORKSPACE_FORBIDDEN',
        message: 'Workspace changed',
      );
    }
  }

  Future<Map<String, dynamic>> read(
    TeamScope scope,
    String path, {
    Map<String, dynamic>? query,
    CancelToken? cancel,
  }) async {
    _check(scope);
    final response = await _dio.get<dynamic>(
      path,
      queryParameters: query,
      cancelToken: cancel,
      options: Options(
        extra: {
          requestScopeUserKey: scope.userId,
          requestScopeWorkspaceKey: scope.workspaceId,
        },
      ),
    );
    _check(scope);
    return asMap(response.data);
  }

  /// [kind] is `agent` or `team`. Omitting [status] lists every status, which
  /// is what the library does; the composer's picker asks for active only.
  Future<Map<String, dynamic>> definitions(
    TeamScope scope,
    String kind, {
    String? status,
    String? cursor,
    CancelToken? cancel,
  }) => read(
    scope,
    '/api/$kind-definitions',
    query: {'status': ?status, 'cursor': ?cursor},
    cancel: cancel,
  );

  Future<Map<String, dynamic>> templates(
    TeamScope scope, {
    String? cursor,
    CancelToken? cancel,
  }) => definitions(
    scope,
    'team',
    status: 'active',
    cursor: cursor,
    cancel: cancel,
  );

  /// Run history across the workspace's projects (§13.8).
  Future<Map<String, dynamic>> runs(
    TeamScope scope, {
    String? status,
    String? projectId,
    String? templateId,
    String? cursor,
    CancelToken? cancel,
  }) => read(
    scope,
    '/api/team-runs',
    query: {
      'status': ?status,
      'project_id': ?projectId,
      'template_id': ?templateId,
      'cursor': ?cursor,
    },
    cancel: cancel,
  );

  Future<TeamSnapshot> snapshot(
    TeamScope scope,
    String runId, {
    CancelToken? cancel,
  }) async => TeamSnapshot.fromJson(
    await read(
      scope,
      '/api/team-runs/${Uri.encodeComponent(runId)}',
      cancel: cancel,
    ),
  );

  Future<void> control(
    TeamScope scope,
    TeamRun run,
    String action,
    String key,
  ) async {
    if (!const ['pause', 'resume', 'cancel'].contains(action)) {
      throw ArgumentError.value(action);
    }
    _check(scope);
    await _dio.post<dynamic>(
      '/api/team-runs/${Uri.encodeComponent(run.id)}/$action',
      data: {'expected_revision': run.revision},
      options: Options(
        headers: {'Idempotency-Key': key},
        extra: {
          requestScopeUserKey: scope.userId,
          requestScopeWorkspaceKey: scope.workspaceId,
        },
      ),
    );
    _check(scope);
  }
}
