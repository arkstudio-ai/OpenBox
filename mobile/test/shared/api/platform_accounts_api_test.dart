import 'dart:async';

import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/http_client.dart';
import 'package:bossip_mobile/shared/api/platform_accounts_api.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:cookie_jar/cookie_jar.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

const scope = (userId: 'user-a', workspaceId: 'workspace-a');

void main() {
  test(
    'every platform call pins workspace and refuses changed identities',
    () async {
      final requests = <RequestOptions>[];
      final auth = AuthSession()..userId = scope.userId;
      final workspace = WorkspaceScope()..currentId = scope.workspaceId;
      final dio = Dio();
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) {
            requests.add(options);
            dynamic data = <String, dynamic>{'id': 'one', 'status': 'pending'};
            if (const [
              '/api/platforms',
              '/api/platform-accounts',
              '/api/publish-jobs',
            ].contains(options.path)) {
              data = <dynamic>[];
            }
            if (options.path.endsWith('/authorize')) {
              data = {'authorizeUrl': 'https://open.douyin.com/'};
            }
            if (options.path == '/api/assets') data = {'items': <dynamic>[]};
            if (options.path.endsWith('/publish')) {
              data = {
                'job': {'id': 'job-a', 'status': 'pending'},
                'schema': 'snssdk1128://openplatform/share?share_type=h5',
              };
            }
            handler.resolve(
              Response(requestOptions: options, statusCode: 200, data: data),
            );
          },
        ),
      );
      final api = PlatformAccountsApi(dio, auth, workspace);
      await api.platforms(scope);
      await api.accounts(scope);
      await api.jobs(scope);
      await api.authorize(scope, 'douyin');
      await api.probe(scope, 'account-a');
      await api.unbind(scope, 'account-a');
      await api.videos(scope);
      await api.job(scope, 'job-a');
      await api.publish(
        scope,
        assetId: 'video',
        title: '中文',
        hashtags: ['话题'],
        privacy: 2,
      );
      expect(requests, hasLength(9));
      for (final request in requests) {
        expect(request.headers['X-Workspace-Id'], scope.workspaceId);
        expect(request.extra[requestScopeUserKey], scope.userId);
      }
      expect(requests.last.data, {
        'file_asset_id': 'video',
        'title': '中文',
        'hashtags': ['话题'],
        'private_status': 2,
        'download_type': 1,
      });
      workspace.currentId = 'workspace-b';
      await expectLater(api.unbind(scope, 'account-a'), throwsStateError);
      workspace.currentId = scope.workspaceId;
      auth.userId = 'user-b';
      await expectLater(api.authorize(scope, 'douyin'), throwsStateError);
      expect(requests, hasLength(9));
    },
  );

  test('a late reply is discarded after workspace changes', () async {
    final sent = Completer<void>();
    final release = Completer<void>();
    final auth = AuthSession()..userId = scope.userId;
    final workspace = WorkspaceScope()..currentId = scope.workspaceId;
    final dio = Dio();
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) async {
          sent.complete();
          await release.future;
          handler.resolve(
            Response(
              requestOptions: options,
              statusCode: 200,
              data: <dynamic>[],
            ),
          );
        },
      ),
    );
    final future = PlatformAccountsApi(dio, auth, workspace).accounts(scope);
    final expectation = expectLater(future, throwsStateError);
    await sent.future;
    workspace.currentId = 'workspace-b';
    release.complete();
    await expectation;
  });

  test('queued request does not receive a different user token', () async {
    final auth = AuthSession()
      ..userId = scope.userId
      ..accessToken = 'test-a';
    final workspace = WorkspaceScope()..currentId = scope.workspaceId;
    final dio = buildApiDio(
      auth: auth,
      cookieJar: CookieJar(),
      workspace: workspace,
    );
    var dispatched = 0;
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          dispatched++;
          handler.resolve(
            Response(
              requestOptions: options,
              statusCode: 200,
              data: <dynamic>[],
            ),
          );
        },
      ),
    );
    final api = PlatformAccountsApi(dio, auth, workspace);
    final call = api.accounts(scope);
    auth
      ..userId = 'user-b'
      ..accessToken = 'test-b';
    await expectLater(
      call,
      throwsA(
        isA<DioException>().having(
          (e) => e.type,
          'type',
          DioExceptionType.cancel,
        ),
      ),
    );
    expect(dispatched, 0);
  });

  test('failed posting is not automatically retried', () async {
    var count = 0;
    final dio = Dio();
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          count++;
          handler.reject(
            DioException(
              requestOptions: options,
              type: DioExceptionType.receiveTimeout,
            ),
          );
        },
      ),
    );
    final api = PlatformAccountsApi(
      dio,
      AuthSession()..userId = scope.userId,
      WorkspaceScope()..currentId = scope.workspaceId,
    );
    await expectLater(
      api.publish(scope, assetId: 'clip', title: '', hashtags: [], privacy: 0),
      throwsA(isA<DioException>()),
    );
    expect(count, 1);
  });
}
