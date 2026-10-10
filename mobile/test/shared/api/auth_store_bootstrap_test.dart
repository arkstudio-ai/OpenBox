import 'dart:convert';
import 'dart:typed_data';

import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/http_client.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/config/env.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:cookie_jar/cookie_jar.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
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

ResponseBody _json(int status, Object body) => ResponseBody.fromString(
  jsonEncode(body),
  status,
  headers: {
    Headers.contentTypeHeader: ['application/json'],
  },
);

final _refreshUri = Uri.parse('${Env.apiBase}/api/auth/refresh');

/// A jar holding the refresh cookie a previous sign-in left behind.
Future<CookieJar> _savedSession() async {
  final jar = CookieJar();
  await jar.saveFromResponse(_refreshUri, [
    Cookie('refresh_token', 'saved-session')..path = '/api/auth',
  ]);
  return jar;
}

Future<List<String>> _cookies(CookieJar jar) async =>
    (await jar.loadForRequest(_refreshUri)).map((c) => c.name).toList();

DioException _offline(RequestOptions options) =>
    DioException.connectionError(requestOptions: options, reason: 'offline');

/// The real auth controller and refresh client over a scripted server.
ProviderContainer _container(
  CookieJar jar,
  Future<ResponseBody> Function(RequestOptions) server,
) {
  final container = ProviderContainer(
    overrides: [
      cookieJarProvider.overrideWithValue(jar),
      // Only for clearAuth's socket teardown; nothing is sent through it.
      apiDioProvider.overrideWithValue(Dio()),
      refreshDioProvider.overrideWith((ref) {
        final dio = buildRefreshDio(
          cookieJar: jar,
          auth: ref.read(authSessionProvider),
          installationId: 'installation-test-123',
        );
        dio.httpClientAdapter = _Adapter(server);
        return dio;
      }),
    ],
  );
  addTearDown(container.dispose);
  return container;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('200 restores the saved account before the first frame', () async {
    final seen = <String, String?>{};
    final container = _container(await _savedSession(), (options) async {
      seen[options.path] = options.headers['Authorization'] as String?;
      return options.path == '/api/auth/refresh'
          ? _json(200, {
              'access_token': 'fresh',
              'mobile_session_id': 'phone-1',
            })
          : _json(200, {'id': 'u1', 'username': 'alice'});
    });

    await container.read(authProvider.notifier).bootstrap();

    final auth = container.read(authProvider);
    expect(auth.isAuthenticated, isTrue);
    expect([auth.isLoading, auth.offline], [false, false]);
    expect(auth.user?.id, 'u1');
    expect(auth.mobileSessionId, 'phone-1');
    expect(container.read(authSessionProvider).accessToken, 'fresh');
    expect(seen['/api/auth/me'], 'Bearer fresh');
  });

  for (final status in [401, 403]) {
    test('$status signs out', () async {
      final jar = await _savedSession();
      final container = _container(
        jar,
        (_) async => _json(status, {'detail': 'Invalid refresh token'}),
      );

      await container.read(authProvider.notifier).bootstrap();

      final auth = container.read(authProvider);
      expect(auth.isAuthenticated, isFalse);
      expect([auth.isLoading, auth.offline], [false, false]);
    });
  }

  for (final code in [
    'AUTH_MOBILE_SESSION_REPLACED',
    'AUTH_MOBILE_LOGIN_REQUIRED',
  ]) {
    test('$code signs out and clears the cookies', () async {
      final jar = await _savedSession();
      final container = _container(
        jar,
        (_) async => _json(401, {
          'detail': {'code': code, 'message': 'Please sign in again.'},
        }),
      );

      await container.read(authProvider.notifier).bootstrap();
      await Future<void>.delayed(Duration.zero);

      final auth = container.read(authProvider);
      expect(auth.isAuthenticated, isFalse);
      expect(auth.offline, isFalse);
      expect(await _cookies(jar), isEmpty);
    });
  }

  final transient = <String, Future<ResponseBody> Function(RequestOptions)>{
    'offline': (options) async => throw _offline(options),
    'a timeout': (options) async => throw DioException.receiveTimeout(
      timeout: const Duration(seconds: 15),
      requestOptions: options,
    ),
    'a 503': (_) async => _json(503, {'detail': 'Service Unavailable'}),
    'a 502 from /me': (options) async => options.path == '/api/auth/refresh'
        ? _json(200, {'access_token': 'fresh'})
        : _json(502, {'detail': 'Bad Gateway'}),
  };
  for (final entry in transient.entries) {
    test('${entry.key} keeps the cookie and waits for a retry', () async {
      final jar = await _savedSession();
      final container = _container(jar, entry.value);

      await container.read(authProvider.notifier).bootstrap();

      final auth = container.read(authProvider);
      expect(auth.isAuthenticated, isFalse);
      expect([auth.isLoading, auth.offline], [false, true]);
      expect(await _cookies(jar), ['refresh_token']);
    });
  }

  test('offline with no saved sign-in is simply signed out', () async {
    final container = _container(
      CookieJar(),
      (options) async => throw _offline(options),
    );

    await container.read(authProvider.notifier).bootstrap();

    final auth = container.read(authProvider);
    expect(auth.isAuthenticated, isFalse);
    expect([auth.isLoading, auth.offline], [false, false]);
  });

  test('retry restores the account once the server answers', () async {
    var online = false;
    final container = _container(await _savedSession(), (options) async {
      if (!online) throw _offline(options);
      return options.path == '/api/auth/refresh'
          ? _json(200, {'access_token': 'fresh'})
          : _json(200, {'id': 'u1', 'username': 'alice'});
    });
    final controller = container.read(authProvider.notifier);
    await controller.bootstrap();
    expect(container.read(authProvider).offline, isTrue);

    final stillOffline = controller.retryRestore();
    expect(container.read(authProvider).isLoading, isTrue);
    await stillOffline;
    expect(container.read(authProvider).offline, isTrue);
    expect(container.read(authProvider).isLoading, isFalse);

    online = true;
    await controller.retryRestore();
    final auth = container.read(authProvider);
    expect(auth.isAuthenticated, isTrue);
    expect([auth.isLoading, auth.offline], [false, false]);
  });

  test(
    'a signed-in user stays signed in when a refresh cannot get through',
    () async {
      final jar = await _savedSession();
      final container = _container(
        jar,
        (options) async => throw _offline(options),
      );
      container
          .read(authProvider.notifier)
          .setAuth('expired', const AuthUser(id: 'u1', username: 'alice'));

      final token = await container.read(authSessionProvider).refresh();

      expect(token, isNull);
      final auth = container.read(authProvider);
      expect(auth.isAuthenticated, isTrue);
      expect(auth.offline, isFalse);
      expect(await _cookies(jar), ['refresh_token']);
    },
  );

  test('the refresh client cannot wait forever for an answer', () {
    final dio = buildRefreshDio(cookieJar: CookieJar());
    addTearDown(dio.close);
    expect(dio.options.receiveTimeout, isNotNull);
    expect(dio.options.receiveTimeout, greaterThan(Duration.zero));
  });
}
