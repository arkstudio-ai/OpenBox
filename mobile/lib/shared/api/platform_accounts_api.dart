import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/platform_account.dart';
import '../models/resource.dart';
import 'auth_session.dart';
import 'providers.dart';
import 'workspace_scope.dart';

class PlatformAccountsApi {
  PlatformAccountsApi(this._dio, this._auth, this._workspace);
  final Dio _dio;
  final AuthSession _auth;
  final WorkspaceScope _workspace;

  void checkScope(PlatformScope scope) {
    if (_auth.userId != scope.userId ||
        _workspace.currentId != scope.workspaceId) {
      throw StateError('Platform request scope changed');
    }
  }

  Future<dynamic> _request(
    PlatformScope scope,
    String path, {
    String method = 'GET',
    Object? data,
    Map<String, dynamic>? query,
    CancelToken? cancel,
  }) async {
    checkScope(scope);
    final response = await _dio.request<dynamic>(
      path,
      data: data,
      queryParameters: query,
      cancelToken: cancel,
      options: Options(
        method: method,
        receiveTimeout: const Duration(seconds: 60),
        headers: {'X-Workspace-Id': scope.workspaceId},
        extra: {
          requestScopeUserKey: scope.userId,
          requestScopeWorkspaceKey: scope.workspaceId,
        },
      ),
    );
    checkScope(scope); // Late replies must not populate another user's UI.
    return response.data;
  }

  Future<List<T>> _list<T>(
    PlatformScope scope,
    String path,
    T Function(Map<String, dynamic>) parse, {
    CancelToken? cancel,
    Map<String, dynamic>? query,
  }) async {
    final data = await _request(scope, path, cancel: cancel, query: query);
    if (data is! List) throw const FormatException('Expected a list');
    return data.whereType<Map<String, dynamic>>().map(parse).toList();
  }

  Future<List<PlatformInfo>> platforms(
    PlatformScope scope, {
    CancelToken? cancel,
  }) => _list(
    scope,
    '/api/platforms',
    PlatformInfo.fromJson,
    cancel: cancel,
    query: {'kinds': 'oauth,desktop'},
  );
  Future<List<PlatformAccount>> accounts(
    PlatformScope scope, {
    CancelToken? cancel,
  }) => _list(
    scope,
    '/api/platform-accounts',
    PlatformAccount.fromJson,
    cancel: cancel,
  );
  Future<List<PublishJob>> jobs(PlatformScope scope, {CancelToken? cancel}) =>
      _list(scope, '/api/publish-jobs', PublishJob.fromJson, cancel: cancel);

  Future<PlatformAccount> openDesktopLogin(
    PlatformScope scope,
    String site, {
    CancelToken? cancel,
  }) async => PlatformAccount.fromJson(
    await _request(
          scope,
          '/api/platform-accounts/desktop/${Uri.encodeComponent(site)}/open',
          method: 'POST',
          cancel: cancel,
        )
        as Map<String, dynamic>,
  );

  Future<List<PlatformAccount>> probeDesktopLogins(
    PlatformScope scope, {
    CancelToken? cancel,
  }) async {
    final data = await _request(
      scope,
      '/api/platform-accounts/desktop/probe',
      method: 'POST',
      cancel: cancel,
    );
    if (data is! List) throw const FormatException('Expected a list');
    return data
        .whereType<Map<String, dynamic>>()
        .map(PlatformAccount.fromJson)
        .toList();
  }

  Future<void> logoutDesktopLogin(
    PlatformScope scope,
    String id, {
    CancelToken? cancel,
  }) async {
    await _request(
      scope,
      '/api/platform-accounts/${Uri.encodeComponent(id)}/logout',
      method: 'POST',
      cancel: cancel,
    );
  }

  Future<PlatformNotificationPage> notifications(
    PlatformScope scope, {
    CancelToken? cancel,
  }) async => PlatformNotificationPage.fromJson(
    await _request(
          scope,
          '/api/notifications',
          query: {'unread': true, 'limit': 20},
          cancel: cancel,
        )
        as Map<String, dynamic>,
  );

  Future<void> markNotificationRead(
    PlatformScope scope,
    String id, {
    CancelToken? cancel,
  }) async {
    await _request(
      scope,
      '/api/notifications/${Uri.encodeComponent(id)}/read',
      method: 'POST',
      cancel: cancel,
    );
  }

  Future<String> authorize(
    PlatformScope scope,
    String platform, {
    CancelToken? cancel,
  }) async {
    final data = await _request(
      scope,
      '/api/platform-accounts/${Uri.encodeComponent(platform)}/authorize',
      method: 'POST',
      cancel: cancel,
    );
    return (data as Map<String, dynamic>)['authorizeUrl'] as String;
  }

  Future<PlatformAccount> probe(
    PlatformScope scope,
    String id, {
    CancelToken? cancel,
  }) async => PlatformAccount.fromJson(
    await _request(
          scope,
          '/api/platform-accounts/${Uri.encodeComponent(id)}/probe',
          method: 'POST',
          cancel: cancel,
        )
        as Map<String, dynamic>,
  );

  Future<void> unbind(
    PlatformScope scope,
    String id, {
    CancelToken? cancel,
  }) async {
    await _request(
      scope,
      '/api/platform-accounts/${Uri.encodeComponent(id)}',
      method: 'DELETE',
      cancel: cancel,
    );
  }

  Future<ResourcePage> videos(
    PlatformScope scope, {
    CancelToken? cancel,
  }) async => ResourcePage.fromJson(
    await _request(
          scope,
          '/api/assets',
          cancel: cancel,
          query: {
            'project': 'all',
            'source': 'all',
            'kind': 'video',
            'sort': 'created',
            'limit': 100,
          },
        )
        as Map<String, dynamic>,
  );

  Future<PublishResult> publish(
    PlatformScope scope, {
    required String assetId,
    required String title,
    required List<String> hashtags,
    required int privacy,
    CancelToken? cancel,
  }) async => PublishResult.fromJson(
    await _request(
          scope,
          '/api/platform-accounts/douyin/publish',
          method: 'POST',
          cancel: cancel,
          data: {
            'file_asset_id': assetId,
            'title': title,
            'hashtags': hashtags,
            'private_status': privacy,
            'download_type': 1,
          },
        )
        as Map<String, dynamic>,
  );

  Future<PublishJob> job(
    PlatformScope scope,
    String id, {
    CancelToken? cancel,
  }) async => PublishJob.fromJson(
    await _request(
          scope,
          '/api/publish-jobs/${Uri.encodeComponent(id)}',
          cancel: cancel,
        )
        as Map<String, dynamic>,
  );
}

final platformAccountsApiProvider = Provider<PlatformAccountsApi>(
  (ref) => PlatformAccountsApi(
    ref.watch(apiDioProvider),
    ref.watch(authSessionProvider),
    ref.watch(workspaceScopeProvider),
  ),
);
