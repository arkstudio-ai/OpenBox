import 'dart:math';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/api/auth_session.dart';
import '../../../shared/api/auth_store.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/api/workspace_scope.dart';
import '../models/admin_data.dart';

part 'fleet_api.dart';
part 'skills_api.dart';
part 'billing_api.dart';
part 'push_api.dart';

/// Installed only below the guarded admin route, not below a workspace role.
final adminScopeProvider = Provider<AdminScope>(
  (ref) => throw StateError('Admin route scope is required'),
);
final adminSkillsChangedProvider = Provider<void Function()>((ref) => () {});
final adminApiProvider = Provider<AdminApi>((ref) {
  final api = AdminApi(
    ref.watch(apiDioProvider),
    ref.watch(authSessionProvider),
    ref.watch(workspaceScopeProvider),
    ref.watch(adminScopeProvider),
    () => ref.read(authProvider).user?.role == 'admin',
  );
  ref.onDispose(api.dispose);
  return api;
}, dependencies: [adminScopeProvider]);

class AdminApi {
  AdminApi(this._dio, this._auth, this._workspace, this.scope, this._allowed);
  final Dio _dio;
  final AuthSession _auth;
  final WorkspaceScope _workspace;
  final AdminScope scope;
  final bool Function() _allowed;
  final _requests = <CancelToken, int>{};
  bool _disposed = false;

  void checkAccess() {
    if (_disposed ||
        !_allowed() ||
        _auth.userId != scope.userId ||
        _workspace.currentId != scope.workspaceId) {
      throw ApiError(
        status: 403,
        code: 'FORBIDDEN',
        message: 'Admin access changed',
      );
    }
  }

  void dispose() {
    _disposed = true;
    for (final token in _requests.keys.toList()) {
      token.cancel('Admin scope disposed');
    }
    _requests.clear();
  }

  Future<dynamic> _request(
    String path, {
    String method = 'GET',
    Map<String, dynamic>? query,
    Object? data,
    CancelToken? cancel,
    ResponseType responseType = ResponseType.json,
  }) async {
    checkAccess();
    final token = cancel ?? CancelToken();
    _requests.update(token, (count) => count + 1, ifAbsent: () => 1);
    try {
      final response = await _dio.request<dynamic>(
        path,
        queryParameters: query,
        data: data,
        cancelToken: token,
        options: Options(
          method: method,
          responseType: responseType,
          contentType: data is FormData ? 'multipart/form-data' : null,
          receiveTimeout: const Duration(seconds: 90),
          // Do not carry a bearer token across a redirect to another origin.
          followRedirects: false,
          headers: {
            if (scope.workspaceId != null) 'X-Workspace-Id': scope.workspaceId,
          },
          extra: {
            requestScopeUserKey: scope.userId,
            requestScopeWorkspaceKey: scope.workspaceId,
          },
        ),
      );
      checkAccess();
      if (token.isCancelled) throw StateError('Cancelled admin request');
      return response.data;
    } finally {
      final count = _requests[token] ?? 0;
      if (count <= 1) {
        _requests.remove(token);
      } else {
        _requests[token] = count - 1;
      }
    }
  }

  Future<AdminRecord> _record(
    String path, {
    String method = 'GET',
    Map<String, dynamic>? query,
    Object? data,
    CancelToken? cancel,
  }) async {
    final value = await _request(
      path,
      method: method,
      query: query,
      data: data,
      cancel: cancel,
    );
    if (value is! Map<String, dynamic>) {
      throw const FormatException('Expected an admin record');
    }
    return AdminRecord(value);
  }

  Future<AdminPage> _page(
    String path,
    Map<String, dynamic> query,
    CancelToken cancel,
  ) async => AdminPage.fromJson(
    (await _record(path, query: query, cancel: cancel)).data,
  );

  String _id(String value) => Uri.encodeComponent(value);

  void _reason(String reason) {
    if (reason.trim().isEmpty || reason.trim().length > 1000) {
      throw ArgumentError('An audit reason is required (1–1000 characters)');
    }
  }
}
