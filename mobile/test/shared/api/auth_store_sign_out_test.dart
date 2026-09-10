import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/logto_session.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:cookie_jar/cookie_jar.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class _FakeLogtoSession implements LogtoSession {
  _FakeLogtoSession({this.failure});

  final Object? failure;
  int signOutCalls = 0;
  LogtoSso? receivedConfig;

  @override
  Future<String> signIn(LogtoSso config, {required bool register}) async =>
      'id-token';

  @override
  Future<void> signOut(LogtoSso? config) async {
    signOutCalls += 1;
    receivedConfig = config;
    if (failure != null) throw failure!;
  }
}

Dio _logoutDio(void Function() onLogout) {
  final dio = Dio(BaseOptions(baseUrl: 'https://ai.bossipai.com.cn'));
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        if (options.path == '/api/auth/logout') onLogout();
        handler.resolve(
          Response<Map<String, dynamic>>(
            requestOptions: options,
            statusCode: 200,
            data: const {'ok': true},
          ),
        );
      },
    ),
  );
  return dio;
}

ProviderContainer _container({
  required LogtoSession logto,
  required void Function() onLogout,
}) {
  const config = LogtoSso(
    endpoint: 'https://auth.bossipai.com.cn',
    appId: 'native-app-id',
  );
  return ProviderContainer(
    overrides: [
      apiDioProvider.overrideWithValue(_logoutDio(onLogout)),
      logtoSsoProvider.overrideWith((ref) async => config),
      logtoSessionProvider.overrideWithValue(logto),
      cookieJarProvider.overrideWithValue(CookieJar()),
    ],
  );
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('signOut ends both the OpenBox and Logto sessions', () async {
    var serverLogoutCalls = 0;
    final logto = _FakeLogtoSession();
    final container = _container(
      logto: logto,
      onLogout: () => serverLogoutCalls += 1,
    );
    addTearDown(container.dispose);
    final controller = container.read(authProvider.notifier);
    controller.setAuth(
      'access-token',
      const AuthUser(id: 'user-1', username: 'alice'),
    );
    container.read(workspaceScopeProvider).currentId = 'workspace-1';

    await controller.signOut();

    expect(serverLogoutCalls, 1);
    expect(logto.signOutCalls, 1);
    expect(logto.receivedConfig?.appId, 'native-app-id');
    expect(container.read(authProvider).isAuthenticated, isFalse);
    expect(container.read(authSessionProvider).accessToken, isNull);
    expect(container.read(workspaceScopeProvider).currentId, isNull);
  });

  test('local state stays signed out when centralized logout fails', () async {
    final logto = _FakeLogtoSession(failure: StateError('browser unavailable'));
    final container = _container(logto: logto, onLogout: () {});
    addTearDown(container.dispose);
    final controller = container.read(authProvider.notifier);
    controller.setAuth(
      'access-token',
      const AuthUser(id: 'user-1', username: 'alice'),
    );

    await controller.signOut();

    expect(logto.signOutCalls, 1);
    expect(container.read(authProvider).isAuthenticated, isFalse);
    expect(container.read(authSessionProvider).accessToken, isNull);
  });
}
