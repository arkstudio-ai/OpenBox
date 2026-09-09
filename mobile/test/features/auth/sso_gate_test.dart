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
  @override
  Future<String> signIn(LogtoSso config, {required bool register}) {
    calls++;
    return result.future;
  }

  @override
  Future<void> signOut(LogtoSso? config) async {}
}

Future<ProviderContainer> _mount(
  WidgetTester tester,
  _Session session, {
  int status = 200,
  Map<String, dynamic>? response,
  bool preferencesFail = false,
}) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'en-US'});
  final prefs = await SharedPreferences.getInstance();
  final bundle = (await tester.runAsync(I18nBundle.load))!;
  final dio = Dio();
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        if (options.path.endsWith('/preferences') && preferencesFail) {
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
      logtoSsoProvider.overrideWith(
        (ref) async => const LogtoSso(
          endpoint: 'https://auth.example.test',
          appId: 'native-test',
        ),
      ),
      logtoSessionProvider.overrideWithValue(session),
    ],
  );
  addTearDown(container.dispose);
  final router = GoRouter(
    initialLocation: '/login',
    routes: [
      GoRoute(
        path: '/login',
        builder: (context, state) => const Scaffold(
          body: SsoGate(register: false, child: Text('password fallback')),
        ),
      ),
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
  return container;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  testWidgets('double tap starts one auth request, success opens workbench', (
    tester,
  ) async {
    final session = _Session();
    final container = await _mount(tester, session, preferencesFail: true);
    await tester.tap(find.byType(FilledButton));
    await tester.tap(find.byType(FilledButton));
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
    await tester.tap(find.byType(FilledButton));
    session.result.completeError(PlatformException(code: 'CANCELED'));
    await tester.pumpAndSettle();
    expect(container.read(authProvider).isAuthenticated, isFalse);
    expect(find.text('password fallback'), findsNothing);
    expect(
      tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
      isNotNull,
    );
    session.result = Completer<String>();
    await tester.tap(find.byType(FilledButton));
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
    testWidgets('$code does not hang and permits a fresh login', (
      tester,
    ) async {
      final session = _Session();
      final container = await _mount(tester, session);
      await tester.tap(find.byType(FilledButton));
      session.result.completeError(PlatformException(code: code));
      await tester.pumpAndSettle();
      expect(container.read(authProvider).isAuthenticated, isFalse);
      expect(find.text('password fallback'), findsOneWidget);
      expect(
        tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
        isNotNull,
      );
    });
  }
  for (final status in [401, 429, 503]) {
    testWidgets(
      'token exchange $status keeps the user signed out and allows retry',
      (tester) async {
        final session = _Session();
        final container = await _mount(tester, session, status: status);
        await tester.tap(find.byType(FilledButton));
        session.result.complete('id-token');
        await tester.pumpAndSettle();
        expect(container.read(authProvider).isAuthenticated, isFalse);
        expect(find.text('password fallback'), findsOneWidget);
        expect(
          tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
          isNotNull,
        );
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
      await tester.tap(find.byType(FilledButton));
      session.result.complete('id-token');
      await tester.pumpAndSettle();
      expect(container.read(authProvider).isAuthenticated, isFalse);
      expect(find.text('password fallback'), findsOneWidget);
    });
  }
}
