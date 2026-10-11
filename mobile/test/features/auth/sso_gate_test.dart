import 'dart:async';

import 'package:bossip_mobile/features/auth/widgets/sso_gate.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/logto_session.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _Session implements LogtoSession {
  Completer<String> result = Completer<String>();
  int calls = 0;

  /// The `register` flag of every sign-in, in order.
  final registers = <bool>[];

  @override
  Future<String> signIn(LogtoSso config, {required bool register}) {
    calls++;
    registers.add(register);
    return result.future;
  }

  @override
  Future<void> signOut(LogtoSso? config) async {}
}

/// GET /api/auth/logto/config of a deployment with native SSO.
const _enabled = <String, dynamic>{
  'enabled': true,
  'endpoint': 'https://auth.example.test',
  'native_app_id': 'native-test',
};

const _start = Key('sso-start');

/// [config] answers GET /api/auth/logto/config through the real
/// `logtoSsoProvider`; returning null makes the request fail (offline).
Future<ProviderContainer> _mount(
  WidgetTester tester,
  _Session session, {
  bool register = false,
  Map<String, dynamic>? Function()? config,
  int status = 200,
  Map<String, dynamic>? response,
  bool preferencesFail = false,
  bool acceptPolicies = true,
  bool consentFail = false,
  List<RequestOptions>? requests,
}) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'en-US'});
  final prefs = await SharedPreferences.getInstance();
  final bundle = (await tester.runAsync(I18nBundle.load))!;
  final dio = Dio();
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        requests?.add(options);
        if (options.path.endsWith('/logto/config')) {
          final reply = (config ?? () => _enabled)();
          if (reply == null) {
            handler.reject(
              DioException.connectionError(
                requestOptions: options,
                reason: 'offline',
              ),
            );
          } else {
            handler.resolve(
              Response(requestOptions: options, statusCode: 200, data: reply),
            );
          }
        } else if (options.path.endsWith('/legal-consent') && consentFail) {
          handler.reject(
            DioException(
              requestOptions: options,
              response: Response(requestOptions: options, statusCode: 503),
            ),
          );
        } else if (options.path.endsWith('/preferences') && preferencesFail) {
          handler.reject(
            DioException(
              requestOptions: options,
              type: DioExceptionType.receiveTimeout,
            ),
          );
        } else if (status != 200) {
          handler.reject(
            DioException(
              requestOptions: options,
              response: Response(requestOptions: options, statusCode: status),
            ),
          );
        } else {
          handler.resolve(
            Response(
              requestOptions: options,
              statusCode: 200,
              data: options.path.endsWith('/preferences')
                  ? <String, dynamic>{}
                  : response ??
                        {
                          'access_token': 'test-token',
                          'user': {'id': 'u1', 'username': 'tester'},
                        },
            ),
          );
        }
      },
    ),
  );
  final container = ProviderContainer(
    overrides: [
      apiDioProvider.overrideWithValue(dio),
      i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
      logtoSessionProvider.overrideWithValue(session),
    ],
  );
  addTearDown(container.dispose);
  Widget screen(bool register) => Scaffold(
    body: SsoGate(register: register, child: const Text('password fallback')),
  );
  final router = GoRouter(
    initialLocation: register ? '/register' : '/login',
    routes: [
      GoRoute(path: '/login', builder: (context, state) => screen(false)),
      GoRoute(path: '/register', builder: (context, state) => screen(true)),
      GoRoute(
        path: '/app',
        builder: (context, state) => const Scaffold(body: Text('workbench')),
      ),
    ],
  );
  addTearDown(router.dispose);
  await tester.pumpWidget(
    UncontrolledProviderScope(
      container: container,
      child: MaterialApp.router(
        routerConfig: router,
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  if (acceptPolicies) {
    await tester.tap(find.byType(Checkbox));
    await tester.pumpAndSettle();
  }
  return container;
}

String _t(ProviderContainer container, String key) =>
    container.read(i18nProvider).t(key);

/// After a failed attempt: an error and the same button to try again —
/// never the password form while the server has SSO on.
void _expectRetry(WidgetTester tester, ProviderContainer container) {
  expect(container.read(authProvider).isAuthenticated, isFalse);
  expect(find.text(_t(container, 'auth:errors.ssoFailed')), findsOneWidget);
  expect(find.text('password fallback'), findsNothing);
  expect(tester.widget<FilledButton>(find.byKey(_start)).onPressed, isNotNull);
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  testWidgets('sign-in and sign-up are two different screens', (tester) async {
    final container = await _mount(tester, _Session(), acceptPolicies: false);
    String t(String key) => _t(container, key);
    expect(find.text(t('auth:loginTitle')), findsOneWidget);
    expect(find.text(t('auth:loginBody')), findsOneWidget);
    expect(find.text(t('auth:ssoSignIn')), findsOneWidget);
    expect(find.text(t('auth:registerTitle')), findsNothing);
    // The SSO sign-in screen does not ask for a password.
    final bundle = container.read(i18nProvider).bundle;
    expect(bundle.lookup('zh-CN', 'auth', 'loginBody'), isNot(contains('密码')));
    expect(t('auth:loginBody'), isNot(contains('password')));

    await tester.tap(find.text(t('auth:noAccount')));
    await tester.pumpAndSettle();
    expect(find.text(t('auth:registerTitle')), findsOneWidget);
    expect(find.text(t('auth:registerBody')), findsOneWidget);
    expect(find.text(t('auth:ssoRegister')), findsOneWidget);
    expect(find.text(t('auth:loginTitle')), findsNothing);
    expect(find.text(t('auth:ssoSignIn')), findsNothing);

    await tester.tap(find.text(t('auth:haveAccount')));
    await tester.pumpAndSettle();
    expect(find.text(t('auth:loginTitle')), findsOneWidget);
  });
  for (final register in [false, true]) {
    testWidgets(
      '${register ? 'sign-up' : 'sign-in'} opens its own Logto page',
      (tester) async {
        final session = _Session();
        final container = await _mount(tester, session, register: register);
        await tester.tap(find.byKey(_start));
        session.result.complete('verified-id-token');
        await tester.pumpAndSettle();
        expect(session.registers, [register]);
        expect(container.read(authProvider).isAuthenticated, isTrue);
        expect(find.text('workbench'), findsOneWidget);
      },
    );
  }
  testWidgets('SSO waits for an explicit policy choice', (tester) async {
    final session = _Session();
    await _mount(tester, session, acceptPolicies: false);
    expect(tester.widget<Checkbox>(find.byType(Checkbox)).value, isFalse);
    expect(tester.widget<FilledButton>(find.byKey(_start)).onPressed, isNull);
    expect(session.calls, 0);
  });
  testWidgets('receipt uses issued token and native version before sign-in', (
    tester,
  ) async {
    final session = _Session();
    final requests = <RequestOptions>[];
    final container = await _mount(
      tester,
      session,
      requests: requests,
      consentFail: true,
    );
    await tester.tap(find.byKey(_start));
    session.result.complete('id-token');
    await tester.pumpAndSettle();
    final receipt = requests.singleWhere(
      (r) => r.path.endsWith('/legal-consent'),
    );
    expect(receipt.headers['Authorization'], 'Bearer test-token');
    expect(receipt.data, {
      'version': '2026-09-28',
      'accepted': true,
      'language': 'en-US',
      'channel': 'native',
    });
    _expectRetry(tester, container);
  });
  testWidgets('double tap starts one auth request, success opens workbench', (
    tester,
  ) async {
    final session = _Session();
    final container = await _mount(tester, session, preferencesFail: true);
    await tester.tap(find.byKey(_start));
    await tester.tap(find.byKey(_start));
    expect(session.calls, 1);
    session.result.complete('verified-id-token');
    await tester.pumpAndSettle();
    expect(find.text('workbench'), findsOneWidget);
    expect(container.read(authProvider).isAuthenticated, isTrue);
  });
  testWidgets('cancel keeps login usable without error or password fallback', (
    tester,
  ) async {
    final session = _Session();
    final container = await _mount(tester, session);
    await tester.tap(find.byKey(_start));
    session.result.completeError(PlatformException(code: 'CANCELED'));
    await tester.pumpAndSettle();
    expect(container.read(authProvider).isAuthenticated, isFalse);
    expect(find.text('password fallback'), findsNothing);
    expect(find.text(_t(container, 'auth:errors.ssoFailed')), findsNothing);
    expect(
      tester.widget<FilledButton>(find.byKey(_start)).onPressed,
      isNotNull,
    );
    session.result = Completer<String>();
    await tester.tap(find.byKey(_start));
    session.result.complete('verified-id-token');
    await tester.pumpAndSettle();
    expect(find.text('workbench'), findsOneWidget);
  });
  for (final code in [
    'NO_BROWSER',
    'CANNOT_RESTORE',
    'FAILED',
    'SECURITY_EXCEPTION',
  ]) {
    testWidgets('$code offers a retry that can still sign in', (tester) async {
      final session = _Session();
      final container = await _mount(tester, session);
      await tester.tap(find.byKey(_start));
      session.result.completeError(PlatformException(code: code));
      await tester.pumpAndSettle();
      _expectRetry(tester, container);

      session.result = Completer<String>();
      await tester.tap(find.byKey(_start));
      await tester.pump();
      expect(find.text(_t(container, 'auth:errors.ssoFailed')), findsNothing);
      session.result.complete('verified-id-token');
      await tester.pumpAndSettle();
      expect(session.calls, 2);
      expect(find.text('workbench'), findsOneWidget);
    });
  }
  for (final status in [401, 429, 503]) {
    testWidgets(
      'token exchange $status keeps the user signed out and allows retry',
      (tester) async {
        final session = _Session();
        final container = await _mount(tester, session, status: status);
        await tester.tap(find.byKey(_start));
        session.result.complete('id-token');
        await tester.pumpAndSettle();
        _expectRetry(tester, container);
      },
    );
  }
  for (final response in <Map<String, dynamic>>[
    {},
    {'access_token': 'token'},
    {
      'user': {'id': 'u1'},
    },
  ]) {
    testWidgets('incomplete token response $response cannot authenticate', (
      tester,
    ) async {
      final session = _Session();
      final container = await _mount(tester, session, response: response);
      await tester.tap(find.byKey(_start));
      session.result.complete('id-token');
      await tester.pumpAndSettle();
      _expectRetry(tester, container);
    });
  }
  testWidgets(
    'an unreachable SSO config offers a retry, not the password form',
    (tester) async {
      var reachable = false;
      final container = await _mount(
        tester,
        _Session(),
        acceptPolicies: false,
        config: () => reachable ? _enabled : null,
      );
      expect(find.text(_t(container, 'auth:loginTitle')), findsOneWidget);
      expect(find.text(_t(container, 'auth:ssoUnavailable')), findsOneWidget);
      expect(find.text('password fallback'), findsNothing);
      expect(find.byType(Checkbox), findsNothing);

      reachable = true;
      await tester.tap(find.byKey(const Key('sso-retry')));
      await tester.pumpAndSettle();
      expect(find.text(_t(container, 'auth:ssoUnavailable')), findsNothing);
      expect(find.byKey(_start), findsOneWidget);
      expect(find.text('password fallback'), findsNothing);
    },
  );
  for (final config in <Map<String, dynamic>>[
    {'enabled': false, 'endpoint': '', 'native_app_id': ''},
    {
      'enabled': true,
      'endpoint': 'https://auth.example.test',
      'native_app_id': '',
    },
  ]) {
    final why = config['enabled'] == true ? 'no native app' : 'SSO off';
    testWidgets('a server reporting $why keeps the password form', (
      tester,
    ) async {
      await _mount(
        tester,
        _Session(),
        acceptPolicies: false,
        config: () => config,
      );
      expect(find.text('password fallback'), findsOneWidget);
      expect(find.byKey(_start), findsNothing);
    });
  }
}
