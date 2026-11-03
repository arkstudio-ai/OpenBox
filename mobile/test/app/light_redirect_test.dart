import 'dart:async';

import 'package:bossip_mobile/app/auth_redirect.dart';
import 'package:bossip_mobile/app/chat_location.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/onboarding/state/onboarding_store.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/api/ui_mode_store.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/models/ui_mode.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

const _alice = AuthUser(id: 'u1', username: 'alice');
const _signedIn = AuthState(user: _alice, isLoading: false);
const _signedOut = AuthState(isLoading: false);

String? _redirect(AuthState auth, String location, UiMode mode) {
  final uri = Uri.parse(location);
  return authRedirect(
    auth: auth,
    location: uri.path,
    uri: uri,
    introSeen: () => true,
    uiMode: () => mode,
  );
}

class _Auth extends AuthController {
  _Auth(this._initial);
  final AuthState _initial;
  @override
  AuthState build() => _initial;
}

class _Workspace extends ActiveWorkspaceController {
  @override
  Future<ActiveWorkspaceState> build() async =>
      const ActiveWorkspaceState(currentId: 'workspace-1');
}

class _Mode extends UiModeController {
  _Mode(this.mode);
  final UiMode mode;
  @override
  UiModeState build() => UiModeState(userId: 'u1', mode: mode, loaded: true);
}

/// Where the app's real router takes a launch at [location] (its
/// `initialLocation` when null), redirects included.
Future<String> _launch(
  WidgetTester tester,
  AuthState auth,
  UiMode mode, {
  String? location,
  String? lastChat,
}) async {
  SharedPreferences.setMockInitialValues({introSeenKey: true});
  final prefs = await SharedPreferences.getInstance();
  if (lastChat != null) {
    await ChatLocationStore(
      prefs,
    ).remember((userId: 'u1', workspaceId: 'workspace-1'), lastChat);
  }
  final container = ProviderContainer(
    overrides: [
      prefsProvider.overrideWithValue(prefs),
      authProvider.overrideWith(() => _Auth(auth)),
      activeWorkspaceProvider.overrideWith(_Workspace.new),
      uiModeProvider.overrideWith(() => _Mode(mode)),
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

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('authRedirect with an interface mode', () {
    test('a Light account opens on the Light home', () {
      expect(_redirect(_signedIn, '/', UiMode.light), '/home');
      expect(_redirect(_signedIn, '/intro', UiMode.light), '/home');
      expect(_redirect(_signedIn, '/app', UiMode.light), '/home');
      expect(_redirect(_signedIn, '/register', UiMode.light), '/home');
      expect(_redirect(_signedIn, '/login', UiMode.light), '/home');
    });

    test('the pages both modes have move to their Light tab, query kept', () {
      expect(
        _redirect(_signedIn, '/app/assistant?intro=all', UiMode.light),
        '/assistant?intro=all',
      );
      expect(
        _redirect(_signedIn, '/app/inbox?category=tasks', UiMode.light),
        '/inbox?category=tasks',
      );
      expect(
        _redirect(
          _signedIn,
          '/login?redirect=${Uri.encodeComponent('/app/inbox?category=tasks')}',
          UiMode.light,
        ),
        '/inbox?category=tasks',
      );
    });

    test('everything else is shared as it is', () {
      for (final location in [
        '/home',
        '/me',
        '/assistant',
        '/inbox',
        '/light/topic/video',
        '/light/s/session-1',
        '/app/s/session-1',
        '/app/settings',
        '/app/billing',
        '/app/cron',
      ]) {
        expect(
          _redirect(_signedIn, location, UiMode.light),
          isNull,
          reason: location,
        );
      }
    });

    test('a pro account never sees the Light tabs', () {
      expect(_redirect(_signedIn, '/home', UiMode.pro), '/app');
      expect(_redirect(_signedIn, '/me', UiMode.pro), '/app');
      expect(_redirect(_signedIn, '/assistant', UiMode.pro), '/app/assistant');
      expect(
        _redirect(_signedIn, '/inbox?category=tasks', UiMode.pro),
        '/app/inbox?category=tasks',
      );
      // Today's behaviour, untouched.
      expect(_redirect(_signedIn, '/', UiMode.pro), '/app');
      expect(_redirect(_signedIn, '/app', UiMode.pro), isNull);
      expect(_redirect(_signedIn, '/app/assistant', UiMode.pro), isNull);
      expect(_redirect(_signedIn, '/light/topic/video', UiMode.pro), isNull);
    });

    test('signed out, the Light pages ask to sign in first', () {
      for (final location in ['/home', '/me', '/light/s/session-1']) {
        expect(
          _redirect(_signedOut, location, UiMode.light),
          '/login?redirect=${Uri.encodeComponent(location)}',
          reason: location,
        );
      }
    });

    test('non-admins fall back to the home of their mode', () {
      expect(_redirect(_signedIn, '/app/admin/fleet', UiMode.light), '/home');
      expect(_redirect(_signedIn, '/app/admin/fleet', UiMode.pro), '/app');
    });
  });

  group('app router', () {
    testWidgets('a Light launch lands on the home, not the last chat', (
      tester,
    ) async {
      expect(
        await _launch(
          tester,
          _signedIn,
          UiMode.light,
          lastChat: '/app/s/fruit-shop',
        ),
        '/home',
      );
      expect(
        await _launch(tester, _signedIn, UiMode.light, location: '/app'),
        '/home',
      );
    });

    testWidgets('a pro launch is what it was', (tester) async {
      expect(
        await _launch(
          tester,
          _signedIn,
          UiMode.pro,
          lastChat: '/app/s/fruit-shop',
        ),
        '/app/s/fruit-shop',
      );
      expect(
        await _launch(tester, _signedIn, UiMode.pro, location: '/home'),
        '/app',
      );
    });

    testWidgets('the Light tabs and topic pages resolve', (tester) async {
      for (final location in [
        '/home',
        '/assistant',
        '/inbox',
        '/me',
        '/light/topic/video',
        '/light/s/session-1',
      ]) {
        expect(
          await _launch(tester, _signedIn, UiMode.light, location: location),
          location,
          reason: location,
        );
      }
    });

    testWidgets('a mode change re-routes the current location', (
      tester,
    ) async {
      SharedPreferences.setMockInitialValues({introSeenKey: true});
      final prefs = await SharedPreferences.getInstance();
      final container = ProviderContainer(
        overrides: [
          prefsProvider.overrideWithValue(prefs),
          authProvider.overrideWith(() => _Auth(_signedIn)),
          activeWorkspaceProvider.overrideWith(_Workspace.new),
          uiModeProvider.overrideWith(() => _Mode(UiMode.pro)),
        ],
      );
      addTearDown(container.dispose);
      final router = container.read(routerProvider);
      addTearDown(router.dispose);
      var notified = 0;
      router.routerDelegate.addListener(() => notified++);
      final completer = Completer<void>();
      router.routeInformationProvider.addListener(() {
        if (!completer.isCompleted) completer.complete();
      });
      await tester.pumpWidget(const SizedBox());
      container.read(uiModeProvider.notifier).state = const UiModeState(
        userId: 'u1',
        mode: UiMode.light,
        loaded: true,
      );
      await tester.pump();
      // The refresh listenable fired: the router will re-run its redirect.
      expect(notified + (completer.isCompleted ? 1 : 0), greaterThan(0));
    });
  });
}
