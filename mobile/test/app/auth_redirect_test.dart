import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:bossip_mobile/app/auth_redirect.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/app/workspace_bootstrap.dart';
import 'package:bossip_mobile/features/onboarding/state/onboarding_store.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/http_client.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/config/env.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:cookie_jar/cookie_jar.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

const _alice = AuthUser(id: 'u1', username: 'alice');
const _signedIn = AuthState(user: _alice, isLoading: false);
const _signedOut = AuthState(isLoading: false);

/// [authRedirect] for a location typed the way a link would carry it.
String? _redirect(AuthState auth, String location, {bool introSeen = false}) {
  final uri = Uri.parse(location);
  return authRedirect(
    auth: auth,
    location: uri.path,
    uri: uri,
    introSeen: () => introSeen,
  );
}

class _Auth extends AuthController {
  _Auth(this._initial);
  final AuthState _initial;
  @override
  AuthState build() => _initial;
}

/// Where the app's real router takes a launch at [location] (its
/// `initialLocation` when null), redirects included.
Future<String> _launch(
  WidgetTester tester,
  AuthState auth, {
  String? location,
  bool introSeen = false,
}) async {
  SharedPreferences.setMockInitialValues({introSeenKey: introSeen});
  final prefs = await SharedPreferences.getInstance();
  final container = ProviderContainer(
    overrides: [
      prefsProvider.overrideWithValue(prefs),
      authProvider.overrideWith(() => _Auth(auth)),
    ],
  );
  addTearDown(container.dispose);
  final router = container.read(routerProvider);
  addTearDown(router.dispose);
  await tester.pumpWidget(const SizedBox());
  final start = location == null
      ? router.routeInformationProvider.value.uri
      : Uri.parse(location);
  final matches = await router.routeInformationParser
      .parseRouteInformationWithDependencies(
        RouteInformation(uri: start),
        tester.element(find.byType(SizedBox)),
      );
  return matches.uri.toString();
}

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

/// Keeps the signed-in branch on its workspace spinner: the gate under test
/// sits in front of it.
class _PendingWorkspace extends ActiveWorkspaceController {
  @override
  Future<ActiveWorkspaceState> build() =>
      Completer<ActiveWorkspaceState>().future;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('authRedirect', () {
    test('a signed-in account skips the landing page and the intro', () {
      expect(_redirect(_signedIn, '/'), '/app');
      expect(_redirect(_signedIn, '/', introSeen: true), '/app');
      expect(_redirect(_signedIn, '/intro'), '/app');
    });

    test('a signed-in account keeps its deep links', () {
      for (final location in [
        '/app',
        '/app/s/session-1',
        '/app/inbox?category=tasks',
        '/invite/token-1',
        '/legal/privacy',
      ]) {
        expect(_redirect(_signedIn, location), isNull, reason: location);
      }
      expect(
        _redirect(_signedIn, '/login?redirect=%2Fapp%2Finbox'),
        '/app/inbox',
      );
      expect(_redirect(_signedIn, '/register'), '/app');
    });

    test(
      'a first signed-out launch shows the intro, then the landing page',
      () {
        expect(_redirect(_signedOut, '/'), '/intro');
        expect(_redirect(_signedOut, '/', introSeen: true), isNull);
        expect(_redirect(_signedOut, '/intro'), isNull);
        expect(_redirect(_signedOut, '/login'), isNull);
        expect(_redirect(_signedOut, '/register'), isNull);
      },
    );

    test('signed out, the app and invitations ask to sign in first', () {
      expect(
        _redirect(_signedOut, '/app/inbox?category=tasks'),
        '/login?redirect=${Uri.encodeComponent('/app/inbox?category=tasks')}',
      );
      expect(
        _redirect(_signedOut, '/invite/token-1'),
        '/login?redirect=${Uri.encodeComponent('/invite/token-1')}',
      );
    });

    test('nothing moves while the session is unknown', () {
      for (final auth in [
        const AuthState(),
        const AuthState(isLoading: false, offline: true),
        const AuthState(offline: true),
      ]) {
        expect(_redirect(auth, '/'), isNull);
        expect(_redirect(auth, '/app/s/session-1'), isNull);
      }
    });

    test('only admins reach the admin console', () {
      expect(_redirect(_signedIn, '/app/admin/fleet'), '/app');
      const admin = AuthState(
        user: AuthUser(id: 'u2', username: 'root', role: 'admin'),
        isLoading: false,
      );
      expect(_redirect(admin, '/app/admin/fleet'), isNull);
    });
  });

  group('app router', () {
    testWidgets('a signed-in launch opens the chat home', (tester) async {
      expect(await _launch(tester, _signedIn), '/app');
      expect(await _launch(tester, _signedIn, introSeen: true), '/app');
    });

    testWidgets('a signed-in launch from a link keeps the link', (
      tester,
    ) async {
      expect(
        await _launch(tester, _signedIn, location: '/app/s/session-1'),
        '/app/s/session-1',
      );
    });

    testWidgets('a first signed-out launch opens the intro', (tester) async {
      expect(await _launch(tester, _signedOut), '/intro');
      expect(await _launch(tester, _signedOut, introSeen: true), '/');
    });

    testWidgets('signed out, an app link goes through sign-in', (tester) async {
      expect(
        await _launch(tester, _signedOut, location: '/app/inbox'),
        '/login?redirect=%2Fapp%2Finbox',
      );
    });
  });

  group('offline launch', () {
    late bool online;
    late int refreshStatus;
    late CookieJar jar;

    Future<ProviderContainer> start(WidgetTester tester) async {
      online = false;
      refreshStatus = 200;
      jar = CookieJar();
      await jar.saveFromResponse(Uri.parse('${Env.apiBase}/api/auth/refresh'), [
        Cookie('refresh_token', 'saved-session')..path = '/api/auth',
      ]);
      SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
      final prefs = await SharedPreferences.getInstance();
      final bundle = (await tester.runAsync(I18nBundle.load))!;
      final container = ProviderContainer(
        overrides: [
          prefsProvider.overrideWithValue(prefs),
          cookieJarProvider.overrideWithValue(jar),
          apiDioProvider.overrideWithValue(Dio()),
          refreshDioProvider.overrideWith((ref) {
            final dio = buildRefreshDio(
              cookieJar: jar,
              auth: ref.read(authSessionProvider),
            );
            dio.httpClientAdapter = _Adapter((options) async {
              if (!online) {
                throw DioException.connectionError(
                  requestOptions: options,
                  reason: 'offline',
                );
              }
              if (options.path.endsWith('/refresh')) {
                return refreshStatus == 200
                    ? _json(200, {'access_token': 'fresh'})
                    : _json(refreshStatus, {'detail': 'Invalid refresh token'});
              }
              return _json(200, {'id': 'u1', 'username': 'alice'});
            });
            return dio;
          }),
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
          activeWorkspaceProvider.overrideWith(_PendingWorkspace.new),
        ],
      );
      addTearDown(container.dispose);
      await tester.runAsync(container.read(authProvider.notifier).bootstrap);
      await tester.pumpWidget(
        UncontrolledProviderScope(
          container: container,
          child: MaterialApp(
            theme: ThemeData(
              extensions: [
                BossipTokens.resolve(
                  BossipThemeName.default_,
                  Brightness.light,
                ),
              ],
            ),
            home: const WorkspaceBootstrap(child: Text('landing page')),
          ),
        ),
      );
      return container;
    }

    /// Taps 重试 and lets the (real-async) refresh round-trip finish.
    Future<void> retry(WidgetTester tester) async {
      await tester.runAsync(() async {
        await tester.tap(find.text('重试'));
        await Future<void>.delayed(const Duration(milliseconds: 50));
      });
      await tester.pump();
    }

    testWidgets(
      'keeps the sign-in and waits on a retry, not the landing page',
      (tester) async {
        final container = await start(tester);
        final auth = container.read(authProvider);
        expect(
          [auth.isAuthenticated, auth.offline, auth.isLoading],
          [false, true, false],
        );
        expect(find.text('landing page'), findsNothing);
        expect(find.text('暂时连不上服务器，登录状态已保留。请检查网络后重试。'), findsOneWidget);

        await retry(tester);
        expect(container.read(authProvider).offline, isTrue);
        expect(find.text('landing page'), findsNothing);

        online = true;
        await retry(tester);
        expect(container.read(authProvider).isAuthenticated, isTrue);
        expect(container.read(authProvider).offline, isFalse);
        expect(find.text('重试'), findsNothing);
      },
    );

    testWidgets('a retry the server refuses ends at the signed-out pages', (
      tester,
    ) async {
      final container = await start(tester);
      online = true;
      refreshStatus = 401;
      await retry(tester);
      expect(container.read(authProvider).isAuthenticated, isFalse);
      expect(container.read(authProvider).offline, isFalse);
      expect(find.text('landing page'), findsOneWidget);
    });
  });
}
