import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class _AuthTransport {
  _AuthTransport({
    required this.refreshResponse,
    this.meResponse,
    this.bootstrapResponse,
  }) : dio = Dio(BaseOptions(baseUrl: 'https://openbox.invalid')) {
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          calls.add('${options.method} ${options.path}');
          switch (options.path) {
            case '/api/auth/refresh':
              return _resolveOrReject(handler, options, refreshResponse);
            case '/api/auth/me':
              return _resolveOrReject(handler, options, meResponse);
            case '/api/auth/bootstrap':
              return _resolveOrReject(handler, options, bootstrapResponse);
          }
          return handler.reject(
            DioException(
              requestOptions: options,
              type: DioExceptionType.unknown,
              error: StateError('Unexpected auth request: ${options.path}'),
            ),
          );
        },
      ),
    );
  }

  final Dio dio;
  final ResponseSpec refreshResponse;
  final ResponseSpec? meResponse;
  final ResponseSpec? bootstrapResponse;
  final List<String> calls = [];

  void _resolveOrReject(
    RequestInterceptorHandler handler,
    RequestOptions options,
    ResponseSpec? spec,
  ) {
    if (spec == null) {
      handler.reject(
        DioException(
          requestOptions: options,
          type: DioExceptionType.unknown,
          error: StateError('No response configured for ${options.path}'),
        ),
      );
      return;
    }
    final response = Response<Map<String, dynamic>>(
      requestOptions: options,
      statusCode: spec.statusCode,
      data: spec.data,
    );
    if (spec.statusCode >= 400) {
      handler.reject(
        DioException(
          requestOptions: options,
          response: response,
          type: DioExceptionType.badResponse,
        ),
      );
      return;
    }
    handler.resolve(response);
  }
}

class ResponseSpec {
  const ResponseSpec(this.statusCode, [this.data]);

  final int statusCode;
  final Map<String, dynamic>? data;
}

ProviderContainer _container(_AuthTransport transport, AuthSession session) {
  return ProviderContainer(
    overrides: [
      refreshDioProvider.overrideWithValue(transport.dio),
      authSessionProvider.overrideWithValue(session),
    ],
  );
}

void main() {
  test(
    'bootstraps the trusted single-user mode when refresh is unavailable',
    () async {
      final transport = _AuthTransport(
        refreshResponse: const ResponseSpec(404),
        bootstrapResponse: const ResponseSpec(200, {
          'mode': 'single_user',
          'user': {'id': 'default', 'username': 'default', 'role': 'admin'},
        }),
      );
      final session = AuthSession();
      final container = _container(transport, session);
      addTearDown(container.dispose);

      await container.read(authProvider.notifier).bootstrap();

      final state = container.read(authProvider);
      expect(state.isLoading, isFalse);
      expect(state.isAuthenticated, isTrue);
      expect(state.user?.id, 'default');
      expect(state.user?.role, 'admin');
      expect(session.accessToken, 'openbox-single-user');
      expect(transport.calls, const [
        'POST /api/auth/refresh',
        'GET /api/auth/bootstrap',
      ]);
    },
  );

  test('does not synthesize authentication for multi-user mode', () async {
    final transport = _AuthTransport(
      refreshResponse: const ResponseSpec(401),
      bootstrapResponse: const ResponseSpec(200, {'mode': 'multi_user'}),
    );
    final session = AuthSession();
    final container = _container(transport, session);
    addTearDown(container.dispose);

    await container.read(authProvider.notifier).bootstrap();

    final state = container.read(authProvider);
    expect(state.isLoading, isFalse);
    expect(state.isAuthenticated, isFalse);
    expect(state.user, isNull);
    expect(session.accessToken, isNull);
    expect(transport.calls, const [
      'POST /api/auth/refresh',
      'GET /api/auth/bootstrap',
    ]);
  });

  test('keeps the JWT refresh and me startup flow unchanged', () async {
    final transport = _AuthTransport(
      refreshResponse: const ResponseSpec(200, {'access_token': 'jwt-token'}),
      meResponse: const ResponseSpec(200, {
        'id': 'alice',
        'username': 'Alice',
        'role': 'user',
      }),
    );
    final session = AuthSession();
    final container = _container(transport, session);
    addTearDown(container.dispose);

    await container.read(authProvider.notifier).bootstrap();

    final state = container.read(authProvider);
    expect(state.isLoading, isFalse);
    expect(state.isAuthenticated, isTrue);
    expect(state.user?.id, 'alice');
    expect(session.accessToken, 'jwt-token');
    expect(transport.calls, const [
      'POST /api/auth/refresh',
      'GET /api/auth/me',
    ]);
  });
}
