import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/http_client.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:cookie_jar/cookie_jar.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

class _Adapter implements HttpClientAdapter {
  _Adapter(this.handle);
  final Future<ResponseBody> Function(RequestOptions) handle;
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) => handle(options);
  @override
  void close({bool force = false}) {}
}

ResponseBody _json(
  int status,
  Object body, {
  Map<String, List<String>>? headers,
}) => ResponseBody.fromString(
  jsonEncode(body),
  status,
  headers: {
    Headers.contentTypeHeader: ['application/json'],
    ...?headers,
  },
);

void main() {
  test(
    'API and refresh requests both identify the same mobile installation',
    () async {
      final auth = AuthSession()..accessToken = 'token';
      final jar = CookieJar();
      final clients = [
        buildApiDio(
          auth: auth,
          cookieJar: jar,
          workspace: WorkspaceScope(),
          installationId: 'installation-test-123',
        ),
        buildRefreshDio(
          cookieJar: jar,
          auth: auth,
          installationId: 'installation-test-123',
        ),
      ];
      for (final dio in clients) {
        dio.httpClientAdapter = _Adapter((options) async {
          expect(options.headers['X-Client-Type'], 'mobile');
          expect(options.headers['X-Installation-Id'], 'installation-test-123');
          return _json(200, {});
        });
        await dio.post<dynamic>('/api/auth/refresh');
        dio.close();
      }
    },
  );

  test(
    'late refresh cannot restore a cookie after logout or overwrite a new login',
    () async {
      final auth = AuthSession();
      final jar = CookieJar();
      final dio = buildRefreshDio(
        cookieJar: jar,
        auth: auth,
        installationId: 'installation-test-123',
      );
      addTearDown(dio.close);
      final sent = Completer<void>();
      final response = Completer<ResponseBody>();
      dio.httpClientAdapter = _Adapter((_) {
        sent.complete();
        return response.future;
      });
      final old = dio.post<dynamic>('/api/auth/refresh');
      final rejected = expectLater(
        old,
        throwsA(
          isA<DioException>().having(
            (e) => e.type,
            'cancelled',
            DioExceptionType.cancel,
          ),
        ),
      );
      await sent.future;
      auth.revision++;
      final uri = Uri.parse('${dio.options.baseUrl}/api/auth/refresh');
      await jar.saveFromResponse(uri, [
        Cookie('refresh_token', 'new-login')..path = '/api/auth',
      ]);
      response.complete(
        _json(
          200,
          {'access_token': 'old'},
          headers: {
            'set-cookie': ['refresh_token=old-login; Path=/api/auth'],
          },
        ),
      );
      await rejected;
      expect((await jar.loadForRequest(uri)).single.value, 'new-login');
    },
  );

  test(
    'replacement is terminal and never starts refresh or retries a write',
    () async {
      final auth = AuthSession()
        ..accessToken = 'old'
        ..userId = 'same-user';
      var refreshes = 0;
      var invalidations = 0;
      var requests = 0;
      auth.refreshFn = () async {
        refreshes++;
        return 'new';
      };
      auth.invalidateFn = (code) {
        expect(code, 'AUTH_MOBILE_SESSION_REPLACED');
        invalidations++;
      };
      final dio = buildApiDio(
        auth: auth,
        cookieJar: CookieJar(),
        workspace: WorkspaceScope(),
      );
      addTearDown(dio.close);
      dio.httpClientAdapter = _Adapter((_) async {
        requests++;
        return _json(401, {
          'detail': {'code': 'AUTH_MOBILE_SESSION_REPLACED'},
        });
      });
      await expectLater(
        dio.post<dynamic>('/api/write'),
        throwsA(isA<DioException>()),
      );
      expect([requests, refreshes, invalidations], [1, 0, 1]);
    },
  );

  for (final code in ['AUTH_MOBILE_SESSION_REPLACED', 'EXPIRED']) {
    test(
      'a late $code from a previous login cannot invalidate or replay the new login',
      () async {
        final auth = AuthSession()
          ..accessToken = 'old'
          ..userId = 'same-user';
        var refreshes = 0;
        var invalidations = 0;
        auth.refreshFn = () async {
          refreshes++;
          return 'new';
        };
        auth.invalidateFn = (_) {
          invalidations++;
        };
        final dio = buildApiDio(
          auth: auth,
          cookieJar: CookieJar(),
          workspace: WorkspaceScope(),
        );
        addTearDown(dio.close);
        final sent = Completer<void>();
        final response = Completer<ResponseBody>();
        dio.httpClientAdapter = _Adapter((_) {
          sent.complete();
          return response.future;
        });
        final rejected = expectLater(
          dio.post<dynamic>('/api/write'),
          throwsA(isA<DioException>()),
        );
        await sent.future;
        auth.revision++;
        auth.accessToken = 'new';
        response.complete(
          _json(401, {
            'detail': {'code': code},
          }),
        );
        await rejected;
        expect([refreshes, invalidations], [0, 0]);
        expect(auth.accessToken, 'new');
      },
    );
  }
}
